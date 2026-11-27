"""Wells Fargo's own wellsfargojobs.com listing, RSS view and native job pages.

Membership authority is the unfiltered server-rendered listing at
https://www.wellsfargojobs.com/en/jobs/ (20 cards per page, ?page=N). The
company publishes an alternate full-content RSS view at
/en/jobs/xml/?rss=true on the same site; it is used as a completeness
cross-check and as an independent copy of each full description. The exact
per-posting apply URL (wd1.myworkdaysite.com) is retained but that Workday
board is never enumerated.

Each detail page carries one schema.org JobPosting JSON-LD block whose
description matches the visible ``article.cms-content`` body; only that body is
the description (nav, related jobs, share links, cookie text and application
questions are excluded). Listing pagination over a live dataset is offset-based
and can repeat/skip rows between requests, so multiple passes are unioned until
the union reconciles with the advertised total.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

OFFICIAL_ENTRY = "https://www.wellsfargojobs.com/en/jobs/"
LISTING_URL = OFFICIAL_ENTRY
RSS_URL = "https://www.wellsfargojobs.com/en/jobs/xml/?rss=true"
HOST = "www.wellsfargojobs.com"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REQUEST_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_SIZE = 20
MAX_LISTING_PASSES = 4
MAX_FINAL_PASSES = 2
MAX_PAGES = 500
JOB_PATH = re.compile(r"^/en/jobs/(r-\d+)(?:/([^/]+))?/?$", re.I)
JOB_ID = re.compile(r"^r-\d+$", re.I)
REQUISITION = re.compile(r"^R-\d+$")
SHOWING = re.compile(r"Showing\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+matching jobs", re.I)
SHOWING_EMPTY = re.compile(r"Showing\s+0(?:\s+to\s+0)?\s+of\s+(\d+)\s+matching jobs", re.I)
SALARY_RANGE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?\s*(?:-|–|—|to)\s*\$\s?[\d,]+(?:\.\d+)?")
POSTING_END = re.compile(r"Posting End Date:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HEADINGS = frozenset({
    "about this role:", "about this role", "job description", "why wells fargo:",
    "why wells fargo", "in this role, you will:", "in this role you will:",
    "in this role you will", "required qualifications:", "required qualifications",
    "desired qualifications:", "desired qualifications", "job expectations:",
    "job expectations", "pay range", "pay range:", "benefits", "benefits:",
    "posting location(s):", "posting location:", "posting locations:",
    "posting location(s)", "posting locations", "locations:", "location:",
    "job location:", "posting end date:", "we value equal opportunity",
    "applicants with disabilities", "drug and alcohol policy",
    "wells fargo recruitment and hiring requirements:",
    "required qualifications for europe, middle east & africa only:",
    "location disclaimer", "job location",
})


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace-normalized visible text of a description body."""
    return re.sub(r"\s+", " ", text_html(html or "")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _short_diff(left: str, right: str, limit: int = 3) -> list[dict]:
    """Verbatim excerpts around the first small source-text differences."""
    if not left or not right or left == right:
        return []
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({
            "kind": tag,
            "article": left[max(0, i1 - 60):i2 + 60],
            "jsonld": right[max(0, j1 - 60):j2 + 60],
        })
        if len(changes) >= limit:
            break
    return changes


def _leaf_headings(soup: BeautifulSoup):
    for node in soup.find_all(["strong", "b"]):
        if node.find(["strong", "b"]):
            continue
        yield node


def _url_check(url: str, *, path: str | None = None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != HOST:
        raise ValueError(f"Wells Fargo response left its official host: {url}")
    if path is not None and parsed.path.rstrip("/") != path.rstrip("/"):
        raise ValueError(f"Wells Fargo response left its inspected path: {url}")
    return parsed


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _url_check(response_url, path="/en/jobs")
    soup = BeautifulSoup(html, "lxml")
    site = soup.select_one('meta[property="og:site_name"]')
    canonical = soup.select_one('link[rel="canonical"]')
    heading = soup.select_one("h1")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if (not site or site.get("content") != "Wells Fargo" or not canonical
            or canonical.get("href") != OFFICIAL_ENTRY
            or not heading or _clean(heading.get_text(" ", strip=True)) != "Search for Jobs"):
        raise ValueError("Wells Fargo official jobs entry identity changed")
    if "Search for Jobs" not in title or "Wells Fargo" not in title:
        raise ValueError("Wells Fargo official jobs entry title changed")
    form = soup.select_one("form#job-filter")
    if form is None:
        raise ValueError("Wells Fargo official jobs entry lost its published filter form")
    rss = [link.get("href") for link in soup.select('link[rel="alternate"]')
           if link.get("type") == "application/rss+xml"]
    if rss != [RSS_URL]:
        raise ValueError("Wells Fargo official jobs entry lost its published jobs RSS alternate")
    filters = {}
    for select in form.select("select[name]"):
        name = select.get("name")
        filters[name] = [
            {"value": option.get("value"), "label": _clean(option.get_text(" ", strip=True))}
            for option in select.select("option")
        ]
    pagesize = form.select_one('input[name="pagesize"]')
    cards = soup.select(".card.card-job")
    if not cards:
        raise ValueError("Wells Fargo official jobs entry has no rendered job cards")
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical.get("href"),
        "h1": _clean(heading.get_text(" ", strip=True)),
        "site_name": site.get("content"),
        "language": (soup.html.get("lang") if soup.html else None),
        "rss_url": rss[0],
        "listing_action": form.get("action"),
        "page_size": int(pagesize.get("value")) if pagesize and pagesize.get("value", "").isdigit() else None,
        "filter_options": filters,
        "cards_on_entry": len(cards),
    }


def parse_listing_page(html: str, response_url: str) -> dict:
    parsed = _url_check(response_url, path="/en/jobs")
    query = parsed.query
    soup = BeautifulSoup(html, "lxml")
    site = soup.select_one('meta[property="og:site_name"]')
    heading = soup.select_one("h1")
    if not site or site.get("content") != "Wells Fargo" or not heading \
            or _clean(heading.get_text(" ", strip=True)) != "Search for Jobs":
        raise ValueError(f"Wells Fargo listing identity changed: {response_url}")
    count = soup.select_one("p.job-count")
    count_text = _clean(count.get_text(" ", strip=True)) if count else ""
    match = SHOWING.search(count_text)
    empty = SHOWING_EMPTY.search(count_text)
    if match is None and empty is None:
        raise ValueError(f"Wells Fargo listing lost its matching-jobs counter: {response_url}")
    if match is not None:
        lo, hi, total = (int(value) for value in match.groups())
    else:
        lo = hi = 0
        total = int(empty.group(1))
    if match is not None and not (1 <= lo <= hi <= total):
        raise ValueError(f"Wells Fargo listing counter is inconsistent: {lo}-{hi} of {total}")
    if empty is not None and soup.select("div.card.card-job"):
        raise ValueError("Wells Fargo listing says zero jobs but rendered cards")
    cards, issues = [], []
    seen = set()
    for position, card in enumerate(soup.select("div.card.card-job")):
        anchor = card.select_one("h2.card-title a")
        identifier = card.select_one(".js-job")
        job_id = (identifier.get("data-id") if identifier else "") or ""
        href = anchor.get("href") if anchor else ""
        title = _clean(anchor.get_text(" ", strip=True)) if anchor else ""
        target = urljoin(response_url, href) if href else ""
        path_match = JOB_PATH.match(urlsplit(target).path) if target else None
        if (not JOB_ID.fullmatch(job_id) or path_match is None
                or path_match.group(1).lower() != job_id.lower() or not title
                or not target.startswith("https://www.wellsfargojobs.com/en/jobs/")):
            raise ValueError(f"Wells Fargo listing card has an inconsistent identity: {href!r} / {job_id!r}")
        if job_id.lower() in seen:
            issues.append(f"duplicate_card:{job_id.lower()}")
        seen.add(job_id.lower())
        meta = {}
        for item in card.select("ul.job-meta > li"):
            use = item.select_one("svg use")
            icon = None
            if use is not None:
                icon = (use.get("xlink:href") or use.get("href") or "").split("#")[-1] or None
            if icon == "map-marker":
                meta["location"] = _clean(item.get_text(" ", strip=True))
            elif icon == "briefcase":
                meta["team"] = _clean(item.get_text(" ", strip=True))
            else:
                meta.setdefault("other", []).append(_clean(item.get_text(" ", strip=True)))
        cards.append({
            "id": job_id.lower(),
            "url": target,
            "title": title,
            "location": meta.get("location"),
            "team": meta.get("team"),
            "position": position,
        })
    if hi - lo + 1 != len(cards):
        issues.append(f"card_count_mismatch:{hi - lo + 1}!={len(cards)}")
    return {
        "url": response_url,
        "query": query,
        "showing": {"from": lo, "to": hi, "total": total},
        "cards": cards,
        "issues": issues,
    }


def parse_rss(text: str, response_url: str = RSS_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != HOST or parsed.path != "/en/jobs/xml/":
        raise ValueError(f"Wells Fargo RSS response came from an unexpected URL: {response_url}")
    soup = BeautifulSoup(text, "xml")
    channel = soup.find("channel")
    if channel is None:
        raise ValueError("Wells Fargo jobs RSS has no channel")
    title = _clean(channel.title.get_text()) if channel.title else ""
    link = _clean(channel.link.get_text()) if channel.link else ""
    if title != "WELLS FARGO BANK jobs feed" or link.rstrip("/") != LISTING_URL.rstrip("/"):
        raise ValueError("Wells Fargo jobs RSS identity changed")
    items, seen, duplicates = [], set(), []
    raw_items = 0
    for node in channel.find_all("item"):
        raw_items += 1
        guid = _clean(node.guid.get_text()) if node.guid else ""
        job_url = _clean(node.link.get_text()) if node.link else ""
        item_title = _clean(node.title.get_text()) if node.title else ""
        description = node.description.get_text() if node.description else ""
        if not JOB_ID.fullmatch(guid):
            raise ValueError(f"Wells Fargo RSS item has an invalid guid: {guid!r}")
        match = JOB_PATH.match(urlsplit(job_url).path)
        if (not job_url.startswith("https://www.wellsfargojobs.com/en/jobs/")
                or match is None or match.group(1).lower() != guid.lower() or not item_title):
            raise ValueError(f"Wells Fargo RSS item has an inconsistent identity: {guid!r} / {job_url!r}")
        if guid.lower() in seen:
            duplicates.append(guid.lower())
            continue
        seen.add(guid.lower())
        pub_date = _clean(node.pubDate.get_text()) if node.pubDate else ""
        items.append({
            "id": guid.lower(),
            "title": item_title,
            "url": job_url,
            "pub_date": pub_date,
            "description": description,
            "description_sha256": sha256(description.encode()).hexdigest(),
            "description_chars": len(normalized_body_text(description)),
        })
    if not items:
        raise ValueError("Wells Fargo jobs RSS exposed no items")
    return {
        "url": response_url,
        "channel_title": title,
        "channel_link": link,
        "raw_item_count": raw_items,
        "items": items,
        "unique_item_count": len(items),
        "duplicates": duplicates,
    }


def _parse_meta(soup: BeautifulSoup) -> dict:
    section = soup.select_one("section#js-job-detail")
    if section is None:
        raise ValueError("Wells Fargo detail page lost its #js-job-detail meta section")
    meta = {"locations": [], "department": None, "employment_type": None,
            "posted_date_raw": None, "posted_date": None, "requisition": None,
            "raw_items": []}
    for item in section.select("ul.job-meta > li"):
        use = item.select_one("svg use")
        icon = (use.get("xlink:href") or use.get("href") or "").split("#")[-1] if use else None
        value = _clean(item.get_text(" ", strip=True))
        meta["raw_items"].append({"icon": icon, "text": value})
        if icon == "map-marker":
            nested = [_clean(node.get_text(" ", strip=True)) for node in item.select("ul.nested > li")]
            meta["locations"] = [value for value in nested if value] or ([value] if value else [])
        elif icon == "briefcase":
            meta["department"] = value or None
        elif icon == "clock":
            meta["employment_type"] = value or None
        elif icon == "calendar":
            time_node = item.select_one("time")
            meta["posted_date_raw"] = value or None
            if time_node is not None:
                meta["posted_date"] = (time_node.get("datetime") or "").strip() or None
        elif icon == "id":
            meta["requisition"] = value or None
    return meta


def _json_ld(soup: BeautifulSoup):
    values = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string if script.string is not None else script.get_text()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            values.append(data)
        elif isinstance(data, list):
            values.extend(item for item in data if isinstance(item, dict))
    return values


def _locations_from_ld(job_posting: dict) -> list[dict]:
    result = []
    for place in job_posting.get("jobLocation") or []:
        if not isinstance(place, dict):
            continue
        addresses = place.get("address")
        if isinstance(addresses, dict):
            addresses = [addresses]
        for address in addresses or []:
            if not isinstance(address, dict):
                continue
            result.append({
                "name": place.get("name") or address.get("name"),
                "locality": address.get("addressLocality"),
                "region": address.get("addressRegion"),
                "country": address.get("addressCountry"),
            })
    return result


def extract_salary(article_html: str) -> dict | None:
    soup = BeautifulSoup(article_html or "", "lxml")
    for heading in _leaf_headings(soup):
        label = _clean(heading.get_text(" ", strip=True))
        if label not in ("Pay Range", "Pay Range:") and not label.startswith("Office Location & Pay Range"):
            continue
        block = heading.find_parent(["p", "li", "div"]) or heading
        parts = [_clean(block.get_text(" ", strip=True))]
        sibling = block
        for _ in range(4):
            sibling = sibling.find_next_sibling()
            if sibling is None or sibling.find(["strong", "b"]):
                break
            text = _clean(sibling.get_text(" ", strip=True))
            if text:
                parts.append(text)
            if SALARY_RANGE.search(" ".join(parts)):
                break
        raw = " ".join(part for part in parts if part).strip()
        if not raw:
            continue
        match = SALARY_RANGE.search(raw)
        return {"section": label, "raw": raw[:1500], "range_raw": match.group(0) if match else None}
    return None


def extract_posting_end_date(description_text: str) -> dict:
    flat = re.sub(r"\s+", " ", description_text or "")
    matches = POSTING_END.findall(flat)
    parsed = []
    for value in matches:
        try:
            parsed.append(datetime.strptime(value, "%d %b %Y").date().isoformat())
        except ValueError:
            continue
    return {"raw": matches, "iso": parsed[0] if parsed else None}


def extract_sections(description_text: str) -> tuple[dict | None, str | None]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (description_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None, None
    folded = [line.casefold() for line in lines]

    def heading(index: int) -> bool:
        return folded[index] in HEADINGS

    def block(start_labels: tuple[str, ...], stop_labels: tuple[str, ...] = ()) -> str | None:
        start = next((i for i, line in enumerate(folded) if line in start_labels), None)
        if start is None:
            return None
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if folded[index] in stop_labels or heading(index):
                end = index
                break
        value = "\n".join(lines[start + 1:end]).strip()
        return value if len(value) >= 20 else None

    required = block(("required qualifications:", "required qualifications"),
                     ("desired qualifications:", "desired qualifications", "job expectations:",
                      "job expectations", "posting location(s):", "posting location:",
                      "posting locations:", "pay range", "pay range:", "benefits", "benefits:",
                      "posting end date:"))
    desired = block(("desired qualifications:", "desired qualifications"),
                    ("job expectations:", "job expectations", "posting location(s):",
                     "posting location:", "posting locations:", "pay range", "pay range:",
                     "benefits", "benefits:", "posting end date:"))
    qualifications = None
    if required or desired:
        qualifications = {}
        if required:
            qualifications["required"] = required
        if desired:
            qualifications["desired"] = desired
    responsibilities = block(
        ("in this role, you will:", "in this role you will:", "in this role you will"),
        ("required qualifications:", "required qualifications", "desired qualifications:",
         "desired qualifications", "job expectations:", "job expectations", "pay range",
         "pay range:", "benefits", "benefits:", "posting location(s):", "posting location:",
         "posting locations:", "posting end date:"))
    return qualifications, responsibilities


def parse_detail_page(html: str, response_url: str, expected_id: str) -> dict:
    parsed = _url_check(response_url)
    match = JOB_PATH.match(parsed.path)
    if match is None or match.group(1).lower() != expected_id.lower():
        raise ValueError(f"Wells Fargo detail URL does not match the listed job {expected_id}: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    detail = soup.select_one("div.job-detail")
    heading = soup.select_one("h1.hero-heading")
    if detail is None or heading is None:
        raise ValueError(f"Wells Fargo detail page lost its native job layout: {response_url}")
    if (detail.get("data-id") or "").lower() != expected_id.lower():
        raise ValueError("Wells Fargo detail page data-id conflicts with the listed job")
    title = _clean(heading.get_text(" ", strip=True))
    if not title:
        raise ValueError("Wells Fargo detail page has no title")
    meta = _parse_meta(soup)
    jobs = [value for value in _json_ld(soup) if value.get("@type") == "JobPosting"]
    issues = []
    if len(jobs) != 1:
        issues.append(f"jobposting_jsonld_count:{len(jobs)}")
    job_posting = jobs[0] if jobs else {}
    if job_posting.get("identifier") and job_posting["identifier"] != meta["requisition"]:
        issues.append(f"jsonld_requisition_conflict:{job_posting['identifier']}!={meta['requisition']}")
    if job_posting.get("identifier") and not REQUISITION.fullmatch(str(job_posting["identifier"])):
        issues.append(f"jsonld_requisition_shape:{job_posting['identifier']}")
    if job_posting.get("title") and _clean(job_posting["title"]) != title:
        issues.append(f"jsonld_title_conflict:{job_posting['title']}")
    article = soup.select_one("article.cms-content")
    article_html = str(article) if article is not None else ""
    ld_html = job_posting.get("description") or ""
    article_text = normalized_body_text(article_html)
    ld_text = normalized_body_text(ld_html)
    if article_html:
        description_html, description_method = article_html, "wellsfargo.job_page.article.cms-content"
    elif ld_html:
        description_html, description_method = ld_html, "wellsfargo.job_page.jobposting_jsonld.description"
    else:
        raise ValueError(f"Wells Fargo detail page exposes no full description: {response_url}")
    description_text = text_html(description_html)
    description_normalized = normalized_body_text(description_html)
    if len(description_normalized) < 100:
        raise ValueError(f"Wells Fargo detail page description is empty or truncated: {response_url}")
    canonical = soup.select_one('link[rel="canonical"]')
    apply_links = [
        {"label": _clean(anchor.get_text(" ", strip=True)), "url": urljoin(response_url, anchor.get("href"))}
        for anchor in detail.select("a.btn-primary")
        if "apply" in _clean(anchor.get_text(" ", strip=True)).casefold()
    ]
    apply_url = apply_links[0]["url"] if apply_links else None
    posted_date = meta["posted_date"]
    if posted_date and not ISO_DATE.match(posted_date):
        posted_date = posted_date[:10] if ISO_DATE.match(posted_date[:10]) else None
    ld_date = job_posting.get("datePosted")
    if not posted_date and isinstance(ld_date, str) and ISO_DATE.match(ld_date[:10]):
        posted_date = ld_date[:10]
    organizations = job_posting.get("hiringOrganization")
    employer = organizations.get("name") if isinstance(organizations, dict) else None
    industry = job_posting.get("industry")
    if isinstance(industry, str):
        industry = [industry]
    qualifications, responsibilities = extract_sections(description_text)
    salary = extract_salary(description_html)
    posting_end = extract_posting_end_date(description_text)
    return {
        "url": response_url,
        "title": title,
        "requisition": meta["requisition"],
        "identifier": job_posting.get("identifier"),
        "locations": _locations_from_ld(job_posting),
        "location_display": meta["locations"],
        "department": meta["department"],
        "industry": industry,
        "employment_type": meta["employment_type"],
        "employment_type_raw": job_posting.get("employmentType"),
        "employer": employer,
        "language": (soup.html.get("lang") if soup.html else None),
        "date_posted": posted_date,
        "date_posted_raw": meta["posted_date_raw"],
        "date_posted_jsonld": ld_date,
        "apply_url": apply_url,
        "apply_links": apply_links,
        "canonical": canonical.get("href") if canonical else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized": description_normalized,
        "description_method": description_method,
        "article_text_chars": len(article_text),
        "jsonld_text_chars": len(ld_text),
        "jsonld_matches_article": bool(article_html and ld_html and article_text == ld_text),
        "jsonld_diff": ([] if not article_html or not ld_html or article_text == ld_text
                        else _short_diff(article_text, ld_text)),
        "salary": salary,
        "valid_through": posting_end["iso"],
        "posting_end_raw": posting_end["raw"],
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "job_posting": job_posting,
        "issues": issues,
    }


class WellsFargo(Adapter):
    """Collect the company's unfiltered jobs listing and every exact native page."""

    VERIFICATION_REVISION = "wellsfargo.official-listing-native-detail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_statuses: Counter = Counter()

    async def _listing_page(self, page: int) -> dict:
        if not self.page_guard():
            raise ValueError("Wells Fargo listing page budget exhausted")
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
        started = time.perf_counter()
        response = await self.http.get(url, headers=REQUEST_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update({
            "request_url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": round(seconds, 6),
            "page": page,
        })
        return parsed

    async def _one_listing_pass(self, pass_number: int) -> tuple[dict, dict]:
        page = 1
        pass_total = None
        records = []
        while True:
            parsed = await self._listing_page(page)
            showing = parsed["showing"]
            if page == 1:
                pass_total = showing["total"]
            records.append({
                "page": page,
                "from": showing["from"],
                "to": showing["to"],
                "total": showing["total"],
                "cards": len(parsed["cards"]),
                "first_id": parsed["cards"][0]["id"] if parsed["cards"] else None,
                "first_url": parsed["cards"][0]["url"] if parsed["cards"] else None,
                "last_id": parsed["cards"][-1]["id"] if parsed["cards"] else None,
                "last_url": parsed["cards"][-1]["url"] if parsed["cards"] else None,
                "new_ids": 0,
                "duplicate_ids": [issue.split(":", 1)[1] for issue in parsed["issues"]
                                  if issue.startswith("duplicate_card:")],
                "issues": [issue for issue in parsed["issues"] if not issue.startswith("duplicate_card:")],
                "seconds": parsed["seconds"],
            })
            for card in parsed["cards"]:
                identifier = card["id"]
                entry = self.listing_union.get(identifier)
                if entry is None:
                    self.listing_union[identifier] = {
                        "id": identifier,
                        "url": card["url"],
                        "title": card["title"],
                        "listing_location": card["location"],
                        "listing_team": card["team"],
                        "positions": [],
                        "first_pass": pass_number,
                        "first_page": page,
                        "first_index": card["position"],
                        "from_rss_only": False,
                    }
                    records[-1]["new_ids"] += 1
                self.listing_union[identifier]["positions"].append({
                    "pass": pass_number, "page": page, "index": card["position"],
                })
            if showing["to"] >= showing["total"] or (showing["to"] == 0 and showing["total"] > 0):
                pass_total = showing["total"]
                break
            page += 1
            if page > MAX_PAGES:
                raise ValueError("Wells Fargo listing exceeds the inspected page budget")
        return {"pass": pass_number, "total": pass_total, "pages": records}, self.listing_union

    async def _rss(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(RSS_URL, headers=REQUEST_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_rss(response.text, str(response.url))
        parsed.update({"status": response.status_code, "final_url": str(response.url),
                       "seconds": round(seconds, 6), "response_bytes": len(response.content)})
        return parsed

    async def detail(self, job):
        started = time.perf_counter()
        listing_id = job.raw["listing_id"]
        try:
            response = await self.http.get(job.url, headers=REQUEST_HEADERS)
        except FetchError as exc:
            message = str(exc)
            code = 404 if "HTTP 404" in message else 410 if "HTTP 410" in message else None
            if code is None:
                raise
            self.detail_statuses[str(code)] += 1
            job.description_unavailable = True
            job.warnings.append(f"detail_http_{code}")
            job.raw.setdefault("detail", {}).update({
                "requested_url": job.url, "status": code, "error": message,
                "seconds": round(max(0.0, time.perf_counter() - started), 6),
            })
            self.result.metadata.setdefault("detail_http_failures", []).append(
                {"id": job.job_id, "url": job.url, "status": code, "error": message})
            return job
        seconds = max(0.0, time.perf_counter() - started)
        self.detail_statuses[str(response.status_code)] += 1
        parsed = parse_detail_page(response.text, str(response.url), expected_id=listing_id)
        job.title = parsed["title"]
        job.requisition_id = parsed["requisition"] or (f"R-{listing_id[2:]}" if listing_id.startswith("r-") else None)
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_sha256 = sha256(parsed["description_normalized"].encode()).hexdigest()
        job.description_method = parsed["description_method"]
        job.description_source_url = str(response.url)
        job.apply_url = parsed["apply_url"]
        job.locations = parsed["locations"] or [{"name": value} for value in parsed["location_display"]]
        job.department = parsed["department"] or (
            parsed["industry"][0] if parsed["industry"] and len(parsed["industry"]) == 1 else parsed["industry"])
        job.employment_type = parsed["employment_type"] or parsed["employment_type_raw"]
        job.employer = parsed["employer"]
        job.language = parsed["language"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"] or parsed["date_posted_jsonld"]
        job.valid_through = parsed["valid_through"]
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job_posting = dict(parsed["job_posting"])
        job_posting.pop("description", None)
        rss = job.raw.get("rss")
        warnings = list(job.warnings)
        comparisons = {}
        if rss:
            rss_sha = rss.get("description_normalized_sha256")
            detail_sha = sha256(parsed["description_normalized"].encode()).hexdigest()
            comparisons["rss_description_matches_detail"] = bool(rss_sha and rss_sha == detail_sha)
            comparisons["rss_description_normalized_sha256"] = rss_sha
            comparisons["detail_description_normalized_sha256"] = detail_sha
            if rss_sha and rss_sha != detail_sha:
                warnings.append("rss_description_differs_from_detail")
            published = _rss_date(rss.get("pub_date"))
            if published and parsed["date_posted"] and published != parsed["date_posted"]:
                warnings.append("rss_pubdate_differs_from_detail_date")
            comparisons["rss_pubdate"] = published
        card_title = _clean(job.raw.get("listing_title"))
        if card_title and _clean(parsed["title"]) != card_title \
                and not card_title.casefold().endswith(_clean(parsed["title"]).casefold()):
            warnings.append("card_title_differs_from_detail")
        if parsed["apply_url"] and job.requisition_id:
            pattern = re.escape(job.requisition_id) + r"(?:-\d+)?(?:[/?#]|$)"
            if not re.search(pattern, parsed["apply_url"], re.I):
                warnings.append("apply_url_identity_mismatch")
        if len(parsed["posting_end_raw"]) > 1:
            warnings.append("multiple_posting_end_dates")
        if "(Evergreen)" in parsed["title"]:
            warnings.append("evergreen_posting")
        job.warnings = list(dict.fromkeys(warnings))
        job.raw["detail"] = {
            "requested_url": job.url,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": round(seconds, 6),
            "title": parsed["title"],
            "meta": {
                "requisition": parsed["requisition"],
                "locations": parsed["location_display"],
                "department": parsed["department"],
                "employment_type": parsed["employment_type"],
                "date_posted_raw": parsed["date_posted_raw"],
                "date_posted": parsed["date_posted"],
            },
            "json_ld": job_posting,
            "json_ld_description_sha256": sha256(parsed["job_posting"].get("description", "").encode()).hexdigest(),
            "json_ld_description_chars": parsed["jsonld_text_chars"],
            "article_description_chars": parsed["article_text_chars"],
            "description_normalized_chars": len(parsed["description_normalized"]),
            "description_normalized_sha256": sha256(parsed["description_normalized"].encode()).hexdigest(),
            "jsonld_matches_article": parsed["jsonld_matches_article"],
            "jsonld_diff": parsed["jsonld_diff"],
            "apply_links": parsed["apply_links"],
            "canonical": parsed["canonical"],
            "posting_end_raw": parsed["posting_end_raw"],
            "issues": parsed["issues"],
            "comparisons": comparisons,
        }
        job.raw["posting_kind"] = "evergreen" if "(Evergreen)" in parsed["title"] else "regular"
        return job

    async def _final_listing_pass(self):
        if not self.page_guard():
            raise ValueError("Wells Fargo final listing page budget exhausted")
        page = 1
        total = None
        ids = set()
        records = []
        while True:
            parsed = await self._listing_page(page)
            showing = parsed["showing"]
            total = showing["total"]
            ids.update(card["id"] for card in parsed["cards"])
            records.append({"page": page, "from": showing["from"], "to": showing["to"],
                            "total": showing["total"], "cards": len(parsed["cards"]),
                            "first_id": parsed["cards"][0]["id"] if parsed["cards"] else None,
                            "first_url": parsed["cards"][0]["url"] if parsed["cards"] else None,
                            "last_id": parsed["cards"][-1]["id"] if parsed["cards"] else None,
                            "last_url": parsed["cards"][-1]["url"] if parsed["cards"] else None})
            if showing["to"] >= showing["total"] or (showing["to"] == 0 and showing["total"] > 0):
                break
            page += 1
            if page > MAX_PAGES:
                raise ValueError("Wells Fargo final listing exceeds the inspected page budget")
        return {"total": total, "ids": ids, "pages": records}

    async def _add_jobs(self, ids, rss_items):
        new_jobs = []
        for identifier in sorted(ids):
            if identifier in self.job_ids:
                continue
            row = self.listing_union.get(identifier) or {}
            rss = rss_items.get(identifier)
            if not row and not rss:
                continue
            title = row.get("title") or rss["title"]
            url = row.get("url") or rss["url"]
            job_id = f"R-{identifier[2:]}" if identifier.startswith("r-") else identifier.upper()
            raw_rss = None
            if rss:
                rss_normalized = normalized_body_text(rss["description"])
                raw_rss = {key: rss[key] for key in (
                    "id", "title", "url", "pub_date", "description_sha256", "description_chars")}
                raw_rss["description_normalized_sha256"] = sha256(rss_normalized.encode()).hexdigest()
                raw_rss["description_normalized_chars"] = len(rss_normalized)
            job = self.job(
                raw={
                    "listing_id": identifier,
                    "listing_title": title,
                    "listing_url": url,
                    "listing_location": row.get("listing_location"),
                    "listing_team": row.get("listing_team"),
                    "listing_positions": row.get("positions", []),
                    "first_pass": row.get("first_pass"),
                    "first_page": row.get("first_page"),
                    "first_index": row.get("first_index"),
                    "membership_source": "listing" if row and row.get("positions") else "rss",
                    "rss": raw_rss,
                },
                title=title,
                url=url,
                job_id=job_id,
                description=(rss.get("description") if rss else ""),
                method=("wellsfargo.jobs_rss.description" if rss else None),
                description_url=(RSS_URL if rss else None),
                department=row.get("listing_team"),
                date_posted=(_rss_date(rss.get("pub_date")) if rss else None),
                date_posted_raw=(rss.get("pub_date") if rss else None),
            )
            if rss:
                job.description_text = text_html(rss.get("description") or "")
            self.add(job)
            self.job_ids.add(identifier)
            new_jobs.append(job)
        return new_jobs

    async def _retry_unavailable(self, job) -> bool:
        try:
            response = await self.http.get(job.url, headers=REQUEST_HEADERS)
        except FetchError as exc:
            message = str(exc)
            return "HTTP 404" in message or "HTTP 410" in message
        return response.status_code in (404, 410)

    async def run(self):
        entry_started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url, headers=REQUEST_HEADERS)
        entry_seconds = max(0.0, time.perf_counter() - entry_started)
        if str(entry.url).rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
            raise ValueError(f"Wells Fargo official entry redirected: {entry.url}")
        entry_meta = parse_official_entry(entry.text, str(entry.url))
        self.result.metadata["official_entry"] = {
            **entry_meta,
            "final_url": str(entry.url),
            "status": entry.status_code,
            "seconds": round(entry_seconds, 6),
            "response_bytes": len(entry.content),
            "response_sha256": sha256(entry.content).hexdigest(),
        }

        self.listing_union: dict[str, dict] = {}
        self.job_ids: set[str] = set()
        listing_started = time.perf_counter()
        pass_records = []
        total = None
        for pass_number in range(1, MAX_LISTING_PASSES + 1):
            record, _ = await self._one_listing_pass(pass_number)
            pass_records.append(record)
            total = record["total"] if record["total"] is not None else total
            if total is not None and len(self.listing_union) >= total:
                break
            if pass_number == MAX_LISTING_PASSES:
                self.result.errors.append(
                    f"wellsfargo_listing_union_stalled_at_{len(self.listing_union)}_of_{total}")
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.timings["listing_phase_seconds"] = listing_seconds
        self.result.expected_total = total
        advertised = [record["total"] for record in pass_records if record["total"] is not None]
        self.result.metadata["listing"] = {
            "url": LISTING_URL,
            "page_size": PAGE_SIZE,
            "passes": pass_records,
            "pass_count": len(pass_records),
            "advertised_totals": advertised,
            "union_ids": len(self.listing_union),
            "listing_seconds": round(listing_seconds, 6),
        }

        rss_started = time.perf_counter()
        rss = await self._rss()
        rss_seconds = max(0.0, time.perf_counter() - rss_started)
        self.result.timings["rss_phase_seconds"] = rss_seconds
        rss_items = {item["id"]: item for item in rss["items"]}
        self.rss = rss
        rss_only = sorted(set(rss_items) - set(self.listing_union))
        for identifier in rss_only:
            item = rss_items[identifier]
            self.listing_union[identifier] = {
                "id": identifier,
                "url": item["url"],
                "title": item["title"],
                "listing_location": None,
                "listing_team": None,
                "positions": [],
                "first_pass": None,
                "first_page": None,
                "first_index": None,
                "from_rss_only": True,
            }
        listing_only = sorted(set(self.listing_union) - set(rss_items))
        self.result.metadata["rss"] = {
            "url": RSS_URL,
            "channel_title": rss["channel_title"],
            "status": rss["status"],
            "final_url": rss["final_url"],
            "response_bytes": rss["response_bytes"],
            "item_count": rss["raw_item_count"],
            "unique_item_count": rss["unique_item_count"],
            "duplicates": rss["duplicates"],
            "request_seconds": rss["seconds"],
            "phase_seconds": round(rss_seconds, 6),
            "rss_only_ids": rss_only,
            "listing_only_ids": listing_only,
        }
        if rss["unique_item_count"] != total:
            self.result.warnings.append(
                f"wellsfargo_rss_unique_{rss['unique_item_count']}_listing_total_{total}")

        jobs = await self._add_jobs(set(self.listing_union), rss_items)
        detail_started = time.perf_counter()
        await self.hydrate(jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["detail_wall_seconds"] = detail_seconds
        detail_seconds_values = [
            entry.get("seconds", 0.0) for entry in
            (job.raw.get("detail") or {} for job in self.result.jobs) if entry
        ]
        self.result.metadata["detail"] = {
            "statuses": dict(self.detail_statuses),
            "requested": len(jobs),
            "http_failures": self.result.metadata.get("detail_http_failures", []),
            "request_seconds_sum": round(sum(detail_seconds_values), 6),
            "request_count": len(detail_seconds_values),
            "wall_seconds": round(detail_seconds, 6),
        }

        final_started = time.perf_counter()
        final_records = []
        for attempt in range(1, MAX_FINAL_PASSES + 1):
            final = await self._final_listing_pass()
            new_ids = sorted(final["ids"] - self.job_ids)
            final_records.append({
                "attempt": attempt,
                "total": final["total"],
                "ids": len(final["ids"]),
                "new_ids": new_ids,
                "missing_ids": sorted(self.job_ids - final["ids"]),
                "pages": final["pages"],
                "page_count": len(final["pages"]),
            })
            if not new_ids:
                break
            new_jobs = await self._add_jobs(set(new_ids), rss_items)
            await self.hydrate(new_jobs, self.detail)
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_listing_phase_seconds"] = final_seconds
        final_total = final_records[-1]["total"] if final_records else total
        final_ids = set(self.job_ids)
        self.result.expected_total = final_total

        removed = []
        for job in list(self.result.jobs):
            detail = job.raw.get("detail") or {}
            if detail.get("status") not in (404, 410):
                continue
            identifier = job.raw["listing_id"]
            if identifier in final_ids:
                self.result.errors.append(f"wellsfargo_listed_job_detail_{detail['status']}:{job.job_id}")
                continue
            if await self._retry_unavailable(job):
                removed.append({
                    "id": job.job_id,
                    "listing_id": identifier,
                    "url": job.url,
                    "status": detail["status"],
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "listing advertises the posting but its native detail page returned "
                              f"HTTP {detail['status']} twice and the final listing pass no longer includes it",
                })
                self.result.jobs.remove(job)
                self.job_ids.discard(identifier)
            else:
                self.result.errors.append(f"wellsfargo_transient_detail_failure:{job.job_id}")
        if removed:
            self.result.metadata["removed_postings"] = removed
            for entry in list(self.result.metadata.get("detail_http_failures", [])):
                if entry["id"] in {item["id"] for item in removed}:
                    self.result.metadata["detail_http_failures"].remove(entry)

        self.result.metadata["final_listing"] = {
            "passes": final_records,
            "advertised_total": final_total,
            "collected_unique_ids": len(self.result.jobs),
            "removed_postings": removed,
        }
        if len(self.result.jobs) != final_total:
            self.result.listing_complete = False
            self.result.errors.append(
                f"wellsfargo_listing_total_mismatch:{len(self.result.jobs)}!={final_total}")
        else:
            self.result.listing_complete = True
        self.result.metadata["membership"] = {
            "union_ids": len(self.listing_union),
            "rss_unique_ids": rss["unique_item_count"],
            "rss_only_ids": rss_only,
            "listing_only_ids": listing_only,
            "collected_job_ids": [job.job_id for job in self.result.jobs],
            "collected_listing_ids": sorted(job.raw["listing_id"] for job in self.result.jobs),
            "removed_ids": [item["id"] for item in removed],
            "final_advertised_total": final_total,
            "reconciled": len(self.result.jobs) == final_total,
        }
        self.result.metadata["worker_type_note"] = (
            "Worker Type is published only as a listing filter; no per-posting worker-type value is "
            "rendered on cards, detail pages or JSON-LD, so no per-job worker type is fabricated.")
        return self.result


def _rss_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None
