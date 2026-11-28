"""Palo Alto Networks' own TalentBrew careers listing and its exact job details.

The published careers entry is ``https://jobs.paloaltonetworks.com/en`` (a
TalentBrew/Radancy site for organisation id 47263).  Its "Search Jobs" journey
is the server-rendered ``/en/search-jobs`` listing: 1,244 results at the time of
inspection, 15 cards per page, 83 pages, no filters applied.  Each card opens
``/en/job/<city>/<slug>/47263/<native job id>``.  The detail page publishes the
complete employer body as ``div.ats-description`` and a schema.org JobPosting
with the native posted date, requisition id, structured locations and employer.
The Workday apply URL is retained as the exact application destination; Workday
is never enumerated as a job board.

The official entry also publishes a "Contractor Positions" link to Palo Alto
Networks' own Magnit contractor talent network
(``https://paloaltonetworks.willhire.co/``).  Its public ``/jobs`` board is
company-scoped ("Palo Alto Networks Talent Network", 20 contractor roles at the
time of inspection, 10 per page).  Those roles are contract assignments at Palo
Alto Networks and are collected as a clearly labelled secondary listing
(``raw.source_listing = "willhire_contractor"``); they are not mixed into the
1,244-role primary count.
"""
from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from html import unescape
import json
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://jobs.paloaltonetworks.com/en"
HOST = "jobs.paloaltonetworks.com"
LISTING_URL = "https://jobs.paloaltonetworks.com/en/search-jobs"
ORG_ID = "47263"
SITE_ID = "62886"
TENANT_ID = "47263"
PAGE_SIZE = 15
DETAIL_METHOD = "palo_alto_networks.talentbrew.ats_description"
CONTRACTOR_ORIGIN = "https://paloaltonetworks.willhire.co"
CONTRACTOR_HOST = "paloaltonetworks.willhire.co"
CONTRACTOR_ENTRY_URL = CONTRACTOR_ORIGIN + "/"
CONTRACTOR_LISTING_URL = CONTRACTOR_ORIGIN + "/jobs"
CONTRACTOR_PAGE_SIZE = 10
CONTRACTOR_METHOD = "palo_alto_networks.willhire.ck_content"
VERIFICATION_REVISION = "palo_alto_networks.talentbrew-ssr+linked-willhire-contractor.1"

JOB_PATH = re.compile(r"^/en/job/(?P<city>[^/]+)/(?P<slug>[^/]+)/(?P<org>\d+)/(?P<job_id>\d+)$")
CONTRACTOR_PATH = re.compile(r"^/jobs/(?P<job_id>\d+)-(?P<slug>[^/]+)$")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ].*")
HEADLINE = re.compile(r"([\d,]+)\s+search results", re.I)
CONTRACTOR_COUNT = re.compile(r"Found\s+([\d,]+)\s+Contractor Jobs", re.I)
SALARY_RANGE = re.compile(
    r"(?P<currency1>\$|USD|€|EUR|£|GBP|CAD|AUD|SGD|INR|₹)\s*"
    r"(?P<minimum>\d[\d,]*(?:\.\d+)?)\s*[-–—]\s*"
    r"(?:(?P<currency2>\$|USD|€|EUR|£|GBP|CAD|AUD|SGD|INR|₹)\s*)?"
    r"(?P<maximum>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<per>/yr|/year|per year|/hour|per hour|/hr|annually)?",
    re.I,
)
CURRENCY = {"$": "USD", "USD": "USD", "€": "EUR", "EUR": "EUR", "£": "GBP",
            "GBP": "GBP", "CAD": "CAD", "AUD": "AUD", "SGD": "SGD",
            "INR": "INR", "₹": "INR"}
RESPONSIBILITY_HEADING = re.compile(r"responsibilit|your impact|what you.ll do|what you will do|key duties", re.I)
QUALIFICATION_HEADING = re.compile(r"qualification|your experience|requirements|what you.ll bring|what we.re looking for", re.I)
MAJOR_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "header", "hr", "li",
    "main", "nav", "ol", "p", "section", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
})


class _Undefined:
    """Marker used only if a source field is literally missing (kept JSON-safe elsewhere)."""


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


QUOTE_CHARACTERS = "\"'`\u2018\u2019\u201c\u201d"


def jsonld_body_variants(raw_html: str | None) -> list[str]:
    """Both serializations a TalentBrew JSON-LD description can legitimately use.

    The normal case is plain HTML.  Some posts are HTML-escaped one extra time
    (``&amp;amp;``, ``&amp;lt;a ...&amp;gt;``); decoding once before parsing gives
    the visible text for those.  The stored job description is always the
    visible ``.ats-description`` — this helper is only for the independent
    JSON-LD cross-check.
    """
    raw = raw_html or ""
    primary = text_html(raw)
    secondary = text_html(unescape(raw))
    return [primary] if secondary == primary else [primary, secondary]


def quote_insensitive(value: str) -> str:
    return normalized(value.translate({ord(character): None for character in QUOTE_CHARACTERS}))


def description_equivalence(visible_text: str, jsonld_html: str | None) -> tuple[bool, str, str]:
    """Classify the visible body against the best JSON-LD serialization.

    Returns (exact, classification, chosen_jsonld_text).  Quote-only differences
    are typographic serialization, not missing role content; ``different`` is
    the only blocking classification.
    """
    visible_compact = normalized(visible_text)
    best = (False, "different", text_html(jsonld_html or ""))
    for candidate in jsonld_body_variants(jsonld_html):
        candidate_compact = normalized(candidate)
        if candidate_compact == visible_compact:
            return True, "exact", candidate
        if quote_insensitive(candidate_compact) == quote_insensitive(visible_compact):
            best = (False, "quotes_only", candidate)
    return best


def _attr_int(element, name: str, label: str) -> int:
    raw = element.get(name) if element is not None else None
    if raw is None:
        raise ValueError(f"Palo Alto Networks {label} is missing {name}")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"Palo Alto Networks {label} {name} is not an integer: {raw!r}") from exc


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find("meta", attrs={"name": name})
    value = node.get("content") if node else None
    return str(value).strip() if value is not None else None


def _repair_jsonld_description(raw: str) -> dict | None:
    """Repair only the known talent-network defect: raw quotes inside the HTML description.

    Magnit's willhire JSON-LD occasionally embeds an unescaped ``"`` from an HTML
    attribute inside the ``description`` string, making the whole block invalid
    JSON.  Everything else is well formed; re-encode only that one string and
    leave every other value untouched.
    """
    marker = re.search(r'"description"\s*:\s*"', raw)
    if not marker:
        return None
    start = marker.end() - 1  # position of the string's opening quote
    closing = re.search(r'"\s*,\s*\n\s*"[A-Za-z@_]+"\s*:', raw[start + 1:])
    if not closing:
        return None
    end = start + 1 + closing.start()  # position of the string's closing quote
    value = raw[start + 1:end]
    repaired = raw[:start] + json.dumps(value) + raw[end + 1:]
    try:
        payload = json.loads(repaired)
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("@type") == "JobPosting":
        payload["_jsonld_repaired"] = "unescaped_quotes_in_description"
        return payload
    return None


def _jsonld(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = _repair_jsonld_description(raw)
            if payload is None:
                raise ValueError("Palo Alto Networks detail JSON-LD is not valid JSON")
        if isinstance(payload, dict) and payload.get("@type") == "JobPosting":
            return payload
    raise ValueError("Palo Alto Networks detail has no schema.org JobPosting")


def _date(value) -> tuple[str | None, str | None]:
    """Return (normalised ISO date, raw value); never substitute a clock date."""
    if value is None or value == "":
        return None, None
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat(), raw
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, raw
    return parsed.date().isoformat(), raw


def _locations(job_location) -> list[dict]:
    entries = job_location if isinstance(job_location, list) else [job_location] if job_location else []
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
        display = ", ".join(str(value) for value in
                             (address.get("addressLocality"), address.get("addressRegion"),
                              address.get("addressCountry")) if value)
        result.append({
            "locality": address.get("addressLocality"),
            "region": address.get("addressRegion"),
            "country": address.get("addressCountry"),
            "street_address": address.get("streetAddress") or None,
            "postal_code": address.get("postalCode") or None,
            "display": display or None,
        })
    return result


def _structured_salary(value) -> dict | list | None:
    entries = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    salaries = []
    for entry in entries:
        currency = normalized(entry.get("currency")) if entry.get("currency") else None
        quantity = entry.get("value") if isinstance(entry.get("value"), dict) else {}
        minimum = quantity.get("minValue")
        maximum = quantity.get("maxValue")
        amount = quantity.get("value")
        if not currency and not any(x not in (None, "") for x in (minimum, maximum, amount)):
            continue
        item = {
            "currency": CURRENCY.get(currency.upper(), currency) if currency else None,
            "minimum": _number(minimum),
            "maximum": _number(maximum),
            "value": _number(amount),
            "unit": normalized(quantity.get("unitText")).lower() or None,
        }
        item = {key: val for key, val in item.items() if val is not None}
        if item:
            salaries.append(item)
    if not salaries:
        return None
    return salaries[0] if len(salaries) == 1 else salaries


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def prose_salary(description_html: str, sections: list[dict] | None = None) -> dict | list | None:
    """Extract the employer-published annual/hourly range from Compensation Disclosure prose."""
    text = text_html(description_html)
    scopes = [section.get("text", "") for section in (sections or [])
              if re.search(r"compensation disclosure|compensation|salary range|pay range", section.get("heading") or "", re.I)]
    if not scopes:
        marker = re.search(r"compensation disclosure", text, re.I)
        scopes = [text[marker.start():]] if marker else []
    found = []
    seen = set()
    for scope in scopes:
        for match in SALARY_RANGE.finditer(scope or ""):
            currency = CURRENCY.get(match.group("currency1").upper(), match.group("currency1"))
            second = match.group("currency2")
            if second and CURRENCY.get(second.upper(), second) != currency:
                continue
            minimum = _number(match.group("minimum"))
            maximum = _number(match.group("maximum"))
            if minimum is None or maximum is None or maximum < minimum:
                continue
            per = (match.group("per") or "").lower()
            unit = "year" if per in {"/yr", "/year", "per year", "annually"} else "hour" if per in {"/hour", "per hour", "/hr"} else None
            line_start = scope.rfind("\n", 0, match.start()) + 1
            line_end = scope.find("\n", match.end())
            source_line = normalized(scope[line_start: line_end if line_end >= 0 else None])
            key = (currency, minimum, maximum, unit, source_line)
            if key in seen:
                continue
            seen.add(key)
            item = {"currency": currency, "minimum": minimum, "maximum": maximum,
                    "unit": unit, "source_text": source_line,
                    "source": "compensation_disclosure_prose"}
            found.append({k: v for k, v in item.items() if v is not None})
    if not found:
        return None
    return found[0] if len(found) == 1 else found


def _inner_text(root: Tag) -> str:
    pieces: list[str] = []

    def visit(node):
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in {"script", "style", "noscript"}:
            return
        if node.name in BLOCK_TAGS:
            pieces.append("\n")
        if node.name == "br":
            pieces.append("\n")
        else:
            for child in node.children:
                visit(child)
        if node.name in BLOCK_TAGS:
            pieces.append("\n")

    visit(root)
    return "".join(pieces)


def description_sections(description_html: str) -> list[dict]:
    """Deterministic heading/content split of the employer body, including bold-only headings."""
    soup = BeautifulSoup(description_html or "", "lxml")
    root = soup.body or soup
    blocks: list[tuple[str, str]] = []

    def is_bold_heading(node: Tag) -> bool:
        if node.name != "p":
            return False
        text = normalized(node.get_text(" ", strip=True))
        if not text or len(text) > 140:
            return False
        bold = node.find(["strong", "b"])
        return bool(bold and normalized(bold.get_text(" ", strip=True)) == text
                    and len(text) > 2 and not text.endswith("."))

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                continue
            if not isinstance(child, Tag) or child.name in {"script", "style", "noscript"}:
                continue
            if child.name in MAJOR_HEADING_TAGS or is_bold_heading(child):
                heading = normalized(child.get_text(" ", strip=True))
                if heading:
                    blocks.append(("heading", heading))
            elif child.name in {"ul", "ol", "table"}:
                text = normalized(child.get_text(" ", strip=True))
                if text:
                    blocks.append(("text", text))
            elif child.name in {"div", "section", "article", "span", "small", "strong", "b", "i", "em"}:
                walk(child)
            elif child.name == "br":
                continue
            else:
                text = normalized(child.get_text(" ", strip=True))
                if text:
                    blocks.append(("text", text))

    walk(root)
    sections: list[dict] = []
    current = None
    for kind, value in blocks:
        if kind == "heading":
            current = {"heading": value, "parts": []}
            sections.append(current)
        else:
            if current is None:
                current = {"heading": None, "parts": []}
                sections.append(current)
            current["parts"].append(value)
    result = []
    for section in sections:
        text = "\n".join(part for part in section["parts"] if part).strip()
        result.append({"heading": section["heading"], "text": text})
    return result


def section_text(sections: list[dict], pattern: re.Pattern) -> str | None:
    texts = [section["text"] for section in sections
             if section.get("heading") and section.get("text") and pattern.search(section["heading"])]
    return "\n\n".join(texts) if texts else None


def parse_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.path.rstrip("/") != "/en":
        raise ValueError("Palo Alto Networks official entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Palo Alto Networks" not in title:
        raise ValueError("Palo Alto Networks official entry title identity changed")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if not canonical_url or canonical_url.rstrip("/") != OFFICIAL_ENTRY:
        raise ValueError("Palo Alto Networks official entry canonical URL changed")
    identity = {
        "host": _meta(soup, "hostName"),
        "full_domain": _meta(soup, "career-site-full-domain"),
        "site_id": _meta(soup, "site-id"),
        "organization_id": _meta(soup, "site-organization-id"),
        "tenant_id": _meta(soup, "site-tenant-id"),
        "search_client_type": _meta(soup, "search-client-type"),
    }
    if (identity["host"] != HOST or identity["full_domain"] != HOST
            or identity["site_id"] != SITE_ID
            or identity["organization_id"] != ORG_ID
            or identity["tenant_id"] != TENANT_ID
            or identity["search_client_type"] != "SearchAsService"):
        raise ValueError(f"Palo Alto Networks official TalentBrew site identity changed: {identity}")
    links = [urljoin(response_url, anchor.get("href")) for anchor in soup.select("a[href]")]
    search_links = [link for link in links if urlsplit(link).path.rstrip("/") == "/en/search-jobs"]
    if not search_links:
        raise ValueError("Palo Alto Networks official entry no longer links its Search Jobs listing")
    contractor_links = [link for link in links if urlsplit(link).hostname == CONTRACTOR_HOST]
    if not contractor_links:
        raise ValueError("Palo Alto Networks official entry no longer links its published contractor board")
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "identity": identity,
        "listing_url": LISTING_URL,
        "search_links": sorted(set(search_links)),
        "contractor_entry_url": CONTRACTOR_ENTRY_URL,
        "contractor_listing_url": CONTRACTOR_LISTING_URL,
        "contractor_links": sorted(set(contractor_links)),
    }


def parse_listing(html: str, response_url: str = LISTING_URL, *, expected_page: int | None = None) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.path.rstrip("/") != "/en/search-jobs":
        raise ValueError("Palo Alto Networks listing response came from an unexpected route")
    query = parse_qs(parsed.query)
    if set(query) - {"p"}:
        raise ValueError(f"Palo Alto Networks listing response has unexpected query: {query}")
    soup = BeautifulSoup(html, "lxml")
    wrapper = soup.select_one("#search-results")
    if wrapper is None:
        raise ValueError("Palo Alto Networks listing has no #search-results wrapper")
    total = _attr_int(wrapper, "data-total-results", "listing")
    total_jobs = _attr_int(wrapper, "data-total-job-results", "listing")
    pages = _attr_int(wrapper, "data-total-pages", "listing")
    current = _attr_int(wrapper, "data-current-page", "listing")
    records = _attr_int(wrapper, "data-records-per-page", "listing")
    if total != total_jobs or total < 0 or pages < 1 or records < 1:
        raise ValueError("Palo Alto Networks listing totals/pagination are inconsistent")
    if expected_page is not None and current != expected_page:
        raise ValueError(f"Palo Alto Networks listing page response identifies page {current}, expected {expected_page}")
    if pages != math.ceil(total / records):
        raise ValueError(f"Palo Alto Networks listing pages {pages} do not match {total}/{records}")
    headline_node = soup.select_one("h1.section29__search-results-headline")
    headline = normalized(headline_node.get_text(" ", strip=True)) if headline_node else ""
    match = HEADLINE.search(headline)
    if not match or int(match.group(1).replace(",", "")) != total:
        raise ValueError(f"Palo Alto Networks listing headline does not match its total: {headline!r}")

    rows = []
    seen = set()
    for anchor in soup.select("a.section29__search-results-link[data-job-id][href]"):
        job_id = str(anchor.get("data-job-id", "")).strip()
        href = anchor.get("href", "")
        path = urlsplit(href).path
        route = JOB_PATH.fullmatch(path)
        if route is None or route.group("org") != ORG_ID or route.group("job_id") != job_id:
            raise ValueError(f"Palo Alto Networks listing exposed an invalid job link: {href!r}")
        if job_id in seen:
            raise ValueError(f"Palo Alto Networks listing page repeats job id {job_id}")
        seen.add(job_id)
        title_node = anchor.select_one("h2.section29__search-results-job-title")
        location_node = anchor.select_one(".section29__result-location")
        category_node = anchor.select_one(".section29__result-category span")
        title = normalized(title_node.get_text(" ", strip=True)) if title_node else ""
        if not title:
            raise ValueError(f"Palo Alto Networks listing job {job_id} has no title")
        rows.append({
            "job_id": job_id,
            "url": urljoin(response_url, href),
            "path": path,
            "title": title,
            "location_text": normalized(location_node.get_text(" ", strip=True)) if location_node else None,
            "category": normalized(category_node.get_text(" ", strip=True)) if category_node else None,
            "listing_page": current,
        })
    expected_rows = records if current < pages else total - records * (pages - 1)
    if len(rows) != expected_rows:
        raise ValueError(f"Palo Alto Networks listing page {current} has {len(rows)} cards, expected {expected_rows}")

    facets = {}
    for ul in soup.select("ul.search-filter-list"):
        legend = ul.find_previous(["h2", "h3", "h4", "legend", "label"])
        name = normalized(legend.get_text(" ", strip=True)) if legend else None
        items = []
        for item in ul.select("li"):
            facet_name = item.select_one(".filter__facet-name")
            facet_count = item.select_one(".filter__facet-count")
            if facet_name:
                items.append({"name": normalized(facet_name.get_text(" ", strip=True)),
                              "count": normalized(facet_count.get_text(" ", strip=True)) if facet_count else None})
        if items:
            facets[name or f"facet_{len(facets) + 1}"] = items
    sort_select = soup.select_one("select[id*=sort-criteria]")
    sort_options = []
    selected_sort = None
    if sort_select:
        for option in sort_select.find_all("option"):
            sort_options.append({"value": option.get("value"),
                                 "label": normalized(option.get_text(" ", strip=True))})
            if "selected" in option.attrs:
                selected_sort = normalized(option.get_text(" ", strip=True))
    return {
        "total": total,
        "pages": pages,
        "current_page": current,
        "records_per_page": records,
        "headline": headline,
        "rows": rows,
        "facets": facets,
        "sort_options": sort_options,
        "selected_sort": selected_sort,
    }


def _job_identity(soup: BeautifulSoup, route: re.Match, job_id: str) -> dict:
    section = soup.select_one("section.job-description")
    if section is None:
        raise ValueError(f"Palo Alto Networks detail {job_id}: native job description section missing")
    data_job_id = str(section.get("data-job-id", "")).strip()
    data_org_id = str(section.get("data-org-id", "")).strip()
    current_meta = _meta(soup, "search-analytics-currentJobId")
    gtm_job_id = _meta(soup, "gtm_jobid")
    if not data_job_id or data_job_id != job_id or route.group("job_id") != job_id:
        raise ValueError(f"Palo Alto Networks detail {job_id}: native job id mismatch ({data_job_id})")
    if data_org_id != ORG_ID:
        raise ValueError(f"Palo Alto Networks detail {job_id}: organization id changed ({data_org_id})")
    if current_meta and current_meta != job_id:
        raise ValueError(f"Palo Alto Networks detail {job_id}: analytics job id mismatch ({current_meta})")
    if gtm_job_id and gtm_job_id != job_id:
        raise ValueError(f"Palo Alto Networks detail {job_id}: GTM job id mismatch ({gtm_job_id})")
    return {"data_job_id": data_job_id, "data_org_id": data_org_id,
            "current_job_id": current_meta, "gtm_job_id": gtm_job_id}


def parse_detail(html: str, response_url: str, *, expected_job_id: str | None = None,
                 listing: dict | None = None) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != HOST:
        raise ValueError("Palo Alto Networks detail response came from an unexpected host")
    route = JOB_PATH.fullmatch(parsed.path)
    if route is None or route.group("org") != ORG_ID:
        raise ValueError(f"Palo Alto Networks detail response has an unexpected path: {parsed.path}")
    job_id = route.group("job_id")
    if expected_job_id is not None and job_id != str(expected_job_id):
        raise ValueError(f"Palo Alto Networks detail redirected to job {job_id}, expected {expected_job_id}")
    soup = BeautifulSoup(html, "lxml")
    ld = _jsonld(soup)
    identity = _job_identity(soup, route, job_id)
    section = soup.select_one("section.job-description")
    ats = section.select_one(".ats-description") if section else None
    if ats is None:
        raise ValueError(f"Palo Alto Networks detail {job_id}: div.ats-description missing")
    description_html = str(ats.decode_contents() or "").strip()
    if not description_html:
        raise ValueError(f"Palo Alto Networks detail {job_id}: description body is empty")
    visible_text = text_html(description_html)
    if len(visible_text) < 100:
        raise ValueError(f"Palo Alto Networks detail {job_id}: description body is too short")
    description_exact, description_equivalence_value, jsonld_text = description_equivalence(
        visible_text, ld.get("description"))

    title_node = soup.select_one("h2.section30__job-title, h1.ajd_header__job-title")
    title = normalized(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        raise ValueError(f"Palo Alto Networks detail {job_id}: title missing")
    meta_title = _meta(soup, "job-tbcn-job-title")
    if normalized(ld.get("title")) != title or (meta_title and normalized(meta_title) != title):
        raise ValueError(f"Palo Alto Networks detail {job_id}: JSON-LD/meta title does not match visible title")
    listing_title_conflict = bool(listing and normalized(listing.get("title")) != title)

    identifier = ld.get("identifier") if isinstance(ld.get("identifier"), dict) else {}
    ld_ref = normalized(identifier.get("value"))
    meta_ref = normalized(_meta(soup, "job-ats-req-id"))
    id_node = soup.select_one(".section30__job-info-id, .ajd_header__job-info-id")
    visible_ref = re.sub(r"^Ref\s*ID\s*:\s*", "", normalized(id_node.get_text(" ", strip=True)), flags=re.I) if id_node else None
    if ld_ref and meta_ref and ld_ref != meta_ref:
        raise ValueError(f"Palo Alto Networks detail {job_id}: requisition mismatch ({ld_ref} vs {meta_ref})")
    if visible_ref and ld_ref and visible_ref != ld_ref:
        raise ValueError(f"Palo Alto Networks detail {job_id}: visible requisition {visible_ref!r} != JSON-LD {ld_ref!r}")
    requisition = ld_ref or meta_ref or visible_ref

    location_node = soup.select_one(".section30__job-info-location, .ajd_header__job-info-location")
    location_text = normalized(location_node.get_text(" ", strip=True)) if location_node else None
    category_node = soup.select_one(".section30__job-info-category, .ajd_header__job-info-category")
    category = normalized(category_node.get_text(" ", strip=True)) if category_node else None
    meta_category = normalized(_meta(soup, "gtm_tbcn_jobcategory"))
    meta_categories = [part.strip() for part in meta_category.split("|") if part.strip()] if meta_category else []
    if category and meta_categories and category not in meta_categories:
        raise ValueError(f"Palo Alto Networks detail {job_id}: visible category != meta category")
    category = category or (meta_categories[0] if meta_categories else None)

    apply_anchor = soup.select_one("a.section30__job-apply[data-apply-url], a.ajd_btn__apply[data-apply-url]")
    apply_url = (apply_anchor.get("data-apply-url") if apply_anchor else None) or _meta(soup, "search-job-apply-url")
    apply_url = urljoin(response_url, apply_url) if apply_url else None

    ld_url = normalized(ld.get("url"))
    if ld_url:
        ld_route = JOB_PATH.fullmatch(urlsplit(ld_url).path)
        if not ld_route or ld_route.group("job_id") != job_id:
            raise ValueError(f"Palo Alto Networks detail {job_id}: JSON-LD url does not identify this job")

    posted_date, posted_raw = _date(ld.get("datePosted"))
    locations = _locations(ld.get("jobLocation"))
    if not locations and location_text:
        locations = [{"display": location_text}]
    employer_org = ld.get("hiringOrganization") if isinstance(ld.get("hiringOrganization"), dict) else {}
    employer = normalized(employer_org.get("name")) or None
    salary = _structured_salary(ld.get("baseSalary"))
    if salary is None:
        salary = prose_salary(description_html)
    sections = description_sections(description_html)

    meta = {name: _meta(soup, name) for name in (
        "hostName", "site-id", "site-organization-id", "site-tenant-id",
        "search-analytics-currentJobId", "gtm_jobid", "gtm_tbcn_jobcategory",
        "gtm_tbcn_jobtitle", "search-job-apply-url", "search-job-feed-id",
        "job-category-ids", "job-location-ids", "job-ats-req-id",
    )}
    jsonld = {key: value for key, value in ld.items() if key != "description"}
    return {
        "job_id": job_id,
        "title": title,
        "url": response_url,
        "requisition_id": requisition,
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": visible_text,
        "locations": locations,
        "location_text": location_text,
        "department": category,
        "date_posted": posted_date,
        "date_posted_raw": posted_raw,
        "employer": employer,
        "salary": salary,
        "sections": sections,
        "responsibilities": section_text(sections, RESPONSIBILITY_HEADING),
        "qualifications": section_text(sections, QUALIFICATION_HEADING),
        "raw": {
            "source_listing": "talentbrew_jobs",
            "detail_job_id": job_id,
            "data_org_id": identity["data_org_id"],
            "listing_page": listing.get("listing_page") if listing else None,
            "listing_title": listing.get("title") if listing else None,
            "listing_location_text": listing.get("location_text") if listing else None,
            "listing_category": listing.get("category") if listing else None,
            "category": category,
            "categories": meta_categories,
            "location_text": location_text,
            "apply_url": apply_url,
            "sections": sections,
            "jsonld": jsonld,
            "meta": {key: value for key, value in meta.items() if value is not None},
            "visible_description_sha256": sha256_text(visible_text),
            "jsonld_description_sha256": sha256_text(jsonld_text),
            "visible_description_compact_sha256": sha256_text(normalized(visible_text)),
            "jsonld_description_compact_sha256": sha256_text(normalized(jsonld_text)),
            "description_text_equal_jsonld": description_exact,
            "description_equivalence": description_equivalence_value,
            "description_equality_normalization": "whitespace_collapsed+entity_unescaped_jsonld",
            "listing_title_conflict": listing_title_conflict,
            "salary_source": ("jsonld_base_salary" if _structured_salary(ld.get("baseSalary")) is not None
                              else "compensation_disclosure_prose" if salary else None),
            "workplace_type_raw": None,
        },
    }


def parse_contractor_listing(html: str, response_url: str = CONTRACTOR_LISTING_URL,
                             *, expected_page: int | None = None) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != CONTRACTOR_HOST or parsed.path.rstrip("/") != "/jobs":
        raise ValueError("Palo Alto Networks contractor listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    text = normalized(soup.get_text(" ", strip=True))
    count_match = CONTRACTOR_COUNT.search(text)
    if not count_match:
        raise ValueError("Palo Alto Networks contractor listing no longer publishes its result count")
    total = int(count_match.group(1).replace(",", ""))
    page_numbers = [int(button.get_text(strip=True)) for button in soup.select("button.paginator-btn")
                    if button.get_text(strip=True).isdigit()]
    pages = max(page_numbers) if page_numbers else 1
    active = [int(button.get_text(strip=True)) for button in soup.select("button.paginator-btn.active")
              if button.get_text(strip=True).isdigit()]
    current = active[0] if active else 1
    if expected_page is not None and current != expected_page:
        raise ValueError(f"Palo Alto Networks contractor listing identifies page {current}, expected {expected_page}")
    if pages != math.ceil(total / CONTRACTOR_PAGE_SIZE):
        raise ValueError(f"Palo Alto Networks contractor pages {pages} do not match {total}/{CONTRACTOR_PAGE_SIZE}")
    rows = []
    seen = set()
    for anchor in soup.select("a.job-title[href]"):
        path = urlsplit(anchor.get("href", "")).path
        route = CONTRACTOR_PATH.fullmatch(path)
        if route is None:
            continue
        job_id = route.group("job_id")
        if job_id in seen:
            raise ValueError(f"Palo Alto Networks contractor listing page repeats job id {job_id}")
        seen.add(job_id)
        card = anchor.find_parent("div", class_=lambda value: value and "job-card" in value)
        texts = [normalized(span.get_text(" ", strip=True)) for span in card.select(".info-text")] if card else []
        title = normalized(anchor.get("title") or anchor.get_text(" ", strip=True))
        if not title:
            raise ValueError(f"Palo Alto Networks contractor job {job_id} has no title")
        location = next((value for value in texts if re.search(r"\(\s*(?:remote|hybrid|on-?site)\s*\)", value, re.I)), None)
        duration = next((value for value in texts if re.search(r"\d+\s+(?:month|week|year)", value, re.I)), None)
        posted_text = next((value for value in texts if re.sub(r"\s+", " ", value).lower().startswith("posted:")), None)
        business_group = card.select_one(".business-group-label") if card else None
        skills = [normalized(chip.get_text(" ", strip=True)) for chip in card.select(".common-skill-chip")] if card else []
        rows.append({
            "job_id": job_id,
            "url": urljoin(response_url, anchor.get("href")),
            "path": path,
            "title": title,
            "location_text": location,
            "business_group": normalized(business_group.get_text(" ", strip=True)) if business_group else None,
            "contract_duration": duration,
            "posted_text": posted_text,
            "skills": skills,
            "listing_page": current,
        })
    expected_rows = CONTRACTOR_PAGE_SIZE if current < pages else total - CONTRACTOR_PAGE_SIZE * (pages - 1)
    if len(rows) != expected_rows:
        raise ValueError(f"Palo Alto Networks contractor page {current} has {len(rows)} cards, expected {expected_rows}")
    return {"total": total, "pages": pages, "current_page": current,
            "records_per_page": CONTRACTOR_PAGE_SIZE, "rows": rows}


def _contractor_info(soup: BeautifulSoup) -> dict:
    info = {}
    for label in soup.select("#guest_job_detail_right_bar .info-label"):
        text = normalized(label.get_text(" ", strip=True))
        value_node = label.find_next("span", class_="info-text")
        if value_node is not None:
            info[text] = normalized(value_node.get_text(" ", strip=True))
    return info


def parse_contractor_detail(html: str, response_url: str, *, expected_job_id: str | None = None,
                            listing: dict | None = None) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != CONTRACTOR_HOST:
        raise ValueError("Palo Alto Networks contractor detail came from an unexpected host")
    route = CONTRACTOR_PATH.fullmatch(parsed.path)
    if route is None:
        raise ValueError(f"Palo Alto Networks contractor detail has an unexpected path: {parsed.path}")
    job_id = route.group("job_id")
    if expected_job_id is not None and job_id != str(expected_job_id):
        raise ValueError(f"Palo Alto Networks contractor detail redirected to {job_id}, expected {expected_job_id}")
    soup = BeautifulSoup(html, "lxml")
    ld = _jsonld(soup)
    identifier = ld.get("identifier") if isinstance(ld.get("identifier"), dict) else {}
    if normalized(identifier.get("value")) != job_id:
        raise ValueError(f"Palo Alto Networks contractor detail {job_id}: identifier mismatch")
    title_node = soup.select_one(".job-details h3.main-title")
    title = normalized(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title or normalized(ld.get("title")) != title:
        raise ValueError(f"Palo Alto Networks contractor detail {job_id}: title mismatch")
    if listing and normalized(listing.get("title")) != title:
        raise ValueError(f"Palo Alto Networks contractor detail {job_id}: listing title != detail title")
    body = soup.select_one(".job-details .ck-content")
    if body is None:
        raise ValueError(f"Palo Alto Networks contractor detail {job_id}: description body missing")
    description_html = str(body.decode_contents() or "").strip()
    visible_text = text_html(description_html)
    if len(visible_text) < 100:
        raise ValueError(f"Palo Alto Networks contractor detail {job_id}: description body is too short")
    description_exact, description_equivalence_value, jsonld_text = description_equivalence(
        visible_text, ld.get("description"))
    info = _contractor_info(soup)
    location_text = info.get("Contractor Job Location")
    workplace = None
    suffix = re.search(r"\(\s*(remote|hybrid|on-?site)\s*\)\s*$", location_text or "", re.I)
    if suffix:
        workplace = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site",
                     "on-site": "On-site"}.get(suffix.group(1).lower().replace("-", ""), None)
    elif normalized(ld.get("jobLocationType")).upper() == "TELECOMMUTE":
        workplace = "Remote"
    posted_date, posted_raw = _date(ld.get("datePosted"))
    valid_through, valid_through_raw = _date(ld.get("validThrough"))
    if valid_through_raw and valid_through_raw.startswith("1970-01-01"):
        valid_through = None
    salary = _structured_salary(ld.get("baseSalary"))
    pay_text = info.get("Pay")
    if salary:
        items = salary if isinstance(salary, list) else [salary]
        for item in items:
            item["source_text"] = pay_text
    locations = _locations(ld.get("jobLocation"))
    if not locations and location_text:
        locations = [{"display": location_text}]
    apply_node = soup.select_one('a[href^="/job-apply/view"]')
    apply_url = urljoin(response_url, apply_node.get("href")) if apply_node else None
    sections = description_sections(description_html)
    meta = {name: _meta(soup, name) for name in ("hostName", "career-site-full-domain")}
    employer_org = ld.get("hiringOrganization") if isinstance(ld.get("hiringOrganization"), dict) else {}
    return {
        "job_id": job_id,
        "title": title,
        "url": response_url,
        "requisition_id": None,
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": visible_text,
        "locations": locations,
        "location_text": location_text,
        "department": listing.get("business_group") if listing else None,
        "employment_type": normalized(ld.get("employmentType")) or "Contractor",
        "workplace_type": workplace,
        "date_posted": posted_date,
        "date_posted_raw": posted_raw,
        "valid_through": valid_through,
        "employer": normalized(employer_org.get("name")) or "Palo Alto Networks",
        "salary": salary,
        "sections": sections,
        "responsibilities": section_text(sections, RESPONSIBILITY_HEADING),
        "qualifications": section_text(sections, QUALIFICATION_HEADING),
        "raw": {
            "source_listing": "willhire_contractor",
            "detail_job_id": job_id,
            "listing_page": listing.get("listing_page") if listing else None,
            "listing_title": listing.get("title") if listing else None,
            "listing_location_text": listing.get("location_text") if listing else None,
            "listing_business_group": listing.get("business_group") if listing else None,
            "contract_duration": listing.get("contract_duration") if listing else info.get("Contract Duration"),
            "listing_posted_text": listing.get("posted_text") if listing else None,
            "skills": listing.get("skills") if listing else [],
            "job_info": info,
            "location_text": location_text,
            "apply_url": apply_url,
            "sections": sections,
            "jsonld": {key: value for key, value in ld.items() if key != "description"},
            "meta": {key: value for key, value in meta.items() if value is not None},
            "visible_description_sha256": sha256_text(visible_text),
            "jsonld_description_sha256": sha256_text(jsonld_text),
            "visible_description_compact_sha256": sha256_text(normalized(visible_text)),
            "jsonld_description_compact_sha256": sha256_text(normalized(jsonld_text)),
            "description_text_equal_jsonld": description_exact,
            "description_equivalence": description_equivalence_value,
            "description_equality_normalization": "whitespace_collapsed+entity_unescaped_jsonld",
            "salary_source": "jsonld_base_salary" if salary else None,
            "valid_through_raw": valid_through_raw,
            "workplace_type_raw": location_text,
        },
    }


class PaloAltoNetworks(Adapter):
    """Unfiltered official listing membership + exact native detail bodies."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        parsed = urlsplit(self.source.url)
        if parsed.scheme != "https" or parsed.hostname != HOST or not parsed.path.rstrip("/").startswith("/en"):
            raise ValueError(f"Palo Alto Networks dedicated flow must start from its official entry {OFFICIAL_ENTRY}")

    async def _entry(self):
        response = await self.http.get(OFFICIAL_ENTRY)
        return response, parse_entry(response.text, str(response.url))

    async def _listing(self, page: int):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?p={page}"
        response = await self.http.get(url)
        return response, parse_listing(response.text, str(response.url), expected_page=page)

    async def _contractor_listing(self, page: int):
        url = CONTRACTOR_LISTING_URL if page == 1 else f"{CONTRACTOR_LISTING_URL}?page={page}"
        response = await self.http.get(url)
        return response, parse_contractor_listing(response.text, str(response.url), expected_page=page)

    async def _detail(self, job):
        response = await self.http.get(job.url)
        fields = parse_detail(response.text, str(response.url), expected_job_id=job.job_id,
                              listing=job.raw.get("listing"))
        return self._job_from_detail(fields)

    async def _contractor_detail(self, job):
        response = await self.http.get(job.url)
        fields = parse_contractor_detail(response.text, str(response.url), expected_job_id=job.job_id,
                                         listing=job.raw.get("listing"))
        return self._job_from_detail(fields)

    def _job_from_detail(self, fields):
        from ..models import Job
        return Job(
            company=self.company.name,
            source=self.source.adapter,
            title=fields["title"],
            url=fields["url"],
            job_id=fields["job_id"],
            requisition_id=fields.get("requisition_id"),
            apply_url=fields.get("apply_url"),
            description_html=fields["description_html"],
            description_text=fields["description_text"],
            locations=fields.get("locations") or [],
            department=fields.get("department"),
            employment_type=fields.get("employment_type"),
            workplace_type=fields.get("workplace_type"),
            date_posted=fields.get("date_posted"),
            date_posted_raw=fields.get("date_posted_raw"),
            valid_through=fields.get("valid_through"),
            salary=fields.get("salary"),
            qualifications=fields.get("qualifications"),
            responsibilities=fields.get("responsibilities"),
            employer=fields.get("employer"),
            description_source_url=fields["url"],
            description_method=CONTRACTOR_METHOD if fields["raw"]["source_listing"] == "willhire_contractor" else DETAIL_METHOD,
            raw=fields["raw"],
            warnings=(["listing_detail_title_conflict"] if fields["raw"].get("listing_title_conflict") else [])
            + (["description_jsonld_text_mismatch"]
               if fields["raw"].get("description_text_equal_jsonld") is False else []),
        )

    async def run(self):
        self.result.metadata["verification_revision"] = VERIFICATION_REVISION
        self.result.metadata["membership_authority"] = (
            "Palo Alto Networks' official TalentBrew /en/search-jobs SSR listing (all 83 pages, "
            "15 per page, no filters) plus the contractor board explicitly linked from the official "
            "careers entry (paloaltonetworks.willhire.co/jobs, all 20 roles, 10 per page)."
        )
        started = time.perf_counter()
        entry_started = time.perf_counter()
        entry_response, entry = await self._entry()
        self.result.timings["official_entry_seconds"] = time.perf_counter() - entry_started
        self.result.metadata["official_entry"] = entry
        self.result.metadata["official_entry_response"] = {
            "url": str(entry_response.request.url),
            "final_url": str(entry_response.url),
            "status": entry_response.status_code,
            "bytes": len(entry_response.content),
            "sha256": sha256(entry_response.content).hexdigest(),
        }

        listing_started = time.perf_counter()
        first_response, first = await self._listing(1)
        total = first["total"]
        pages = first["pages"]
        self.result.pages += 1
        listing_rows = {row["job_id"]: row for row in first["rows"]}
        listing_errors = []
        page_totals = {1: total}
        page_sizes = {1: len(first["rows"])}
        last_page_ids = [row["job_id"] for row in first["rows"]] if pages == 1 else []

        for page in range(2, pages + 1):
            if page > self.options.max_pages:
                listing_errors.append(f"listing_page_budget_exhausted_before_{page}")
                break
            try:
                response, parsed = await self._listing(page)
            except Exception as exc:
                listing_errors.append(f"Listing page {page}: {type(exc).__name__}: {exc}")
                continue
            self.result.pages += 1
            page_totals[page] = parsed["total"]
            page_sizes[page] = len(parsed["rows"])
            if parsed["total"] != total or parsed["pages"] != pages:
                listing_errors.append(
                    f"listing_page_{page}_changed_total:{parsed['total']}/{parsed['pages']} vs {total}/{pages}")
            for row in parsed["rows"]:
                if row["job_id"] in listing_rows:
                    listing_errors.append(f"duplicate_listing_job:{row['job_id']}")
                    continue
                listing_rows[row["job_id"]] = row
            if page == pages:
                last_page_ids = [row["job_id"] for row in parsed["rows"]]
        self.result.timings["listing_phase_seconds"] = time.perf_counter() - listing_started

        if listing_errors:
            self.result.warnings.extend(listing_errors)

        # A page short/over due to a concurrent insertion shifts later pages; one
        # additional full pass recovers any IDs that moved across the boundary.
        if len(listing_rows) != total:
            self.result.warnings.append(f"listing_membership_first_pass:{len(listing_rows)}/{total}")
            retry_started = time.perf_counter()
            for page in range(1, pages + 1):
                if page > self.options.max_pages:
                    break
                try:
                    _, parsed = await self._listing(page)
                except Exception as exc:
                    listing_errors.append(f"Recovery listing page {page}: {type(exc).__name__}: {exc}")
                    continue
                self.result.pages += 1
                for row in parsed["rows"]:
                    listing_rows.setdefault(row["job_id"], row)
            self.result.timings["listing_recovery_seconds"] = time.perf_counter() - retry_started

        if len(listing_rows) != total:
            listing_errors.append(f"listing_membership_mismatch:{len(listing_rows)}/{total}")

        listing_complete = (not listing_errors and len(listing_rows) == total
                            and all(size == (PAGE_SIZE if page < pages else total - PAGE_SIZE * (pages - 1))
                                    for page, size in page_sizes.items()))
        self.result.metadata["primary_listing"] = {
            "url": LISTING_URL,
            "total": total,
            "pages": pages,
            "records_per_page": PAGE_SIZE,
            "selected_sort": first["selected_sort"],
            "sort_options": first["sort_options"],
            "facets": first["facets"],
            "initial_page_ids": [row["job_id"] for row in first["rows"]],
            "last_page_ids": last_page_ids,
            "pages_fetched": self.result.pages,
            "page_sizes": {str(key): value for key, value in page_sizes.items()},
            "page_totals": {str(key): value for key, value in page_totals.items()},
        }

        jobs = []
        for row in listing_rows.values():
            from ..models import Job
            jobs.append(Job(
                company=self.company.name,
                source=self.source.adapter,
                title=row["title"],
                url=row["url"],
                job_id=row["job_id"],
                locations=[{"display": row["location_text"]}] if row["location_text"] else [],
                department=row["category"],
                raw={"source_listing": "talentbrew_jobs", "listing": row,
                     "listing_page": row["listing_page"],
                     "listing_title": row["title"],
                     "listing_location_text": row["location_text"],
                     "listing_category": row["category"]},
            ))
        if jobs and not self.options.max_jobs:
            await self.hydrate(jobs, self._detail)
        for job in jobs:
            self.add(job)
        self.result.metadata["primary_jobs"] = len(jobs)

        contractor_complete = False
        contractor_total = None
        contractor_rows = {}
        contractor_errors = []
        if not self.options.max_jobs:
            contractor_started = time.perf_counter()
            try:
                contractor_entry_response = await self.http.get(CONTRACTOR_ENTRY_URL)
                contractor_entry_soup = BeautifulSoup(contractor_entry_response.text, "lxml")
                contractor_entry_title = (contractor_entry_soup.title.get_text(" ", strip=True)
                                          if contractor_entry_soup.title else "")
                if "Palo Alto Networks" not in contractor_entry_title:
                    raise ValueError("contractor entry title does not identify Palo Alto Networks")
                if not contractor_entry_soup.select('a[href="/jobs"]'):
                    raise ValueError("contractor entry no longer links its /jobs listing")
                self.result.metadata["contractor_entry"] = {
                    "url": str(contractor_entry_response.url),
                    "title": contractor_entry_title,
                    "status": contractor_entry_response.status_code,
                }
                first_contractor_response, first_contractor = await self._contractor_listing(1)
                contractor_total = first_contractor["total"]
                contractor_pages = first_contractor["pages"]
                contractor_rows = {row["job_id"]: row for row in first_contractor["rows"]}
                self.result.pages += 1
                for page in range(2, contractor_pages + 1):
                    _, parsed = await self._contractor_listing(page)
                    self.result.pages += 1
                    if parsed["total"] != contractor_total:
                        contractor_errors.append(
                            f"contractor_total_changed:{parsed['total']} vs {contractor_total}")
                    for row in parsed["rows"]:
                        if row["job_id"] in contractor_rows:
                            contractor_errors.append(f"duplicate_contractor_job:{row['job_id']}")
                            continue
                        contractor_rows[row["job_id"]] = row
                if len(contractor_rows) != contractor_total:
                    contractor_errors.append(f"contractor_membership_mismatch:{len(contractor_rows)}/{contractor_total}")
                contractor_jobs = []
                for row in contractor_rows.values():
                    from ..models import Job
                    contractor_jobs.append(Job(
                        company=self.company.name,
                        source=self.source.adapter,
                        title=row["title"],
                        url=row["url"],
                        job_id=row["job_id"],
                        locations=[{"display": row["location_text"]}] if row["location_text"] else [],
                        department=row["business_group"],
                        employment_type="Contractor",
                        raw={"source_listing": "willhire_contractor", "listing": row,
                             "listing_page": row["listing_page"],
                             "listing_title": row["title"],
                             "listing_location_text": row["location_text"],
                             "listing_business_group": row["business_group"],
                             "contract_duration": row["contract_duration"]},
                    ))
                if contractor_jobs:
                    await self.hydrate(contractor_jobs, self._contractor_detail)
                for job in contractor_jobs:
                    self.add(job)
                contractor_complete = not contractor_errors and len(contractor_rows) == contractor_total
                self.result.metadata["contractor_listing"] = {
                    "url": CONTRACTOR_LISTING_URL,
                    "total": contractor_total,
                    "pages": contractor_pages,
                    "records_per_page": CONTRACTOR_PAGE_SIZE,
                    "job_ids": list(contractor_rows),
                }
            except Exception as exc:
                contractor_errors.append(f"contractor_board: {type(exc).__name__}: {exc}")
                self.result.errors.append(f"Contractor board {CONTRACTOR_LISTING_URL}: {type(exc).__name__}: {exc}")
            finally:
                self.result.timings["contractor_phase_seconds"] = time.perf_counter() - contractor_started
        if contractor_errors:
            self.result.warnings.extend(contractor_errors)

        # Final membership recheck against the live page 1 and last page.
        recheck_started = time.perf_counter()
        final_errors = []
        try:
            _, final_first = await self._listing(1)
            self.result.pages += 1
            if final_first["total"] != total or final_first["pages"] != pages:
                final_errors.append(f"final_listing_total_changed:{final_first['total']}/{final_first['pages']}")
            final_page1_ids = [row["job_id"] for row in final_first["rows"]]
            if final_page1_ids != [row["job_id"] for row in first["rows"]]:
                final_errors.append("final_listing_page1_ids_changed")
            self.result.metadata["final_primary_page1_ids"] = final_page1_ids
            if pages > 1:
                _, final_last = await self._listing(pages)
                self.result.pages += 1
                final_last_ids = [row["job_id"] for row in final_last["rows"]]
                if final_last_ids != last_page_ids:
                    final_errors.append("final_listing_last_page_ids_changed")
                self.result.metadata["final_primary_last_page_ids"] = final_last_ids
        except Exception as exc:
            final_errors.append(f"final_primary_recheck: {type(exc).__name__}: {exc}")
        if contractor_total is not None:
            try:
                _, final_contractor = await self._contractor_listing(1)
                self.result.pages += 1
                if final_contractor["total"] != contractor_total:
                    final_errors.append(
                        f"final_contractor_total_changed:{final_contractor['total']} vs {contractor_total}")
            except Exception as exc:
                final_errors.append(f"final_contractor_recheck: {type(exc).__name__}: {exc}")
        self.result.timings["final_membership_seconds"] = time.perf_counter() - recheck_started
        if final_errors:
            self.result.errors.extend(final_errors)
            self.result.warnings.extend(final_errors)

        expected_total = total + (contractor_total or 0)
        self.result.expected_total = expected_total
        self.result.listing_complete = bool(listing_complete and all(
            job.description_verified for job in self.result.jobs
        ) and len(self.result.jobs) == expected_total and not final_errors
            and (contractor_complete if contractor_total is not None else True))
        if not listing_complete:
            self.result.errors.append(f"listing_incomplete:{len(listing_rows)}/{total}")
        if contractor_total is None:
            self.result.errors.append("contractor_board_listing_unavailable")
        elif not contractor_complete:
            self.result.errors.append(f"contractor_listing_incomplete:{len(contractor_rows)}/{contractor_total}")
        self.result.metadata["membership"] = {
            "primary_advertised": total,
            "primary_collected": len(listing_rows),
            "contractor_advertised": contractor_total,
            "contractor_collected": len(contractor_rows),
            "expected_total": expected_total,
            "collected_total": len(self.result.jobs),
            "primary_listing_complete": listing_complete,
            "contractor_listing_complete": contractor_complete,
        }
        self.result.finish()
        self.result.timings["company_collection_wall_seconds"] = time.perf_counter() - started
        return self.result
