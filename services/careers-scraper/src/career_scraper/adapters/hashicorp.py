"""HashiCorp's current published careers journey.

HashiCorp's own careers page (``https://www.hashicorp.com/en/careers``) no
longer hosts a job board.  Every "Open positions"/"View open positions" link
points at IBM's careers search pre-filtered with the keyword ``hashicorp``; the
``/en/careers/open-positions`` route 308-redirects to that same search.  The
listing is therefore the exact IBM search membership (``scopeid=careers2``,
``appid=careers``, ``q=hashicorp``), and details are the exact IBM JobDetail
pages for those exposed jobIds only.

Round 3 (user scope decision 2026-11-08): HashiCorp was absorbed into IBM.
Only parent-board postings explicitly attributed to HashiCorp are included:
(a) a structured field (legal employer ``Company`` metadata, or a search
facet/source field) whose value names HashiCorp, or -- where no such field
value exists (the IBM board exposes no company/business-unit facet; every
legal employer is an IBM entity) -- (b) an explicit ``HashiCorp`` in the
job title (listing/search title or detail ``Job Title``), or (c) the role's
own description header (the ``Introduction`` section, or the opening of
``Your role and responsibilities``) naming HashiCorp as the hiring business
via an explicit hiring-attribution phrase (see ``HEADER_PATTERNS``).  Roles
that merely cite HashiCorp products as a skill, preferred certification, or
partner-team product are excluded.  See ``classify_round3_attribution``.

The detail host is behind an AWS WAF JavaScript challenge for plain HTTP, so
details are fetched in one WAF-warmed headless Chromium session.  The listing
search API itself is plain JSON over HTTP.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html

LEDGER_ENTRY_URL = "https://www.hashicorp.com/en/career"
ENTRY_URL = "https://www.hashicorp.com/en/careers"
OPEN_POSITIONS_URL = ENTRY_URL + "/open-positions"
SEARCH_ENTRY_URL = "https://www.ibm.com/careers/search?q=hashicorp"
SEARCH_API_URL = "https://www-api.ibm.com/search/api/v2"
DETAIL_HOST = "careers.ibm.com"
WIDGET_ID = "jobDetailWidget"
SEARCH_PAGE_SIZE = 30
DETAIL_QUERY_URL = "https://careers.ibm.com/careers/JobDetail?jobId={job_id}"


# Exact body observed from IBM's running careers search app when the official
# HashiCorp page was opened.  The aggs are reporting-only; membership comes
# from the keyword query and the returned jobIds.
_SEARCH_BODY = {
    "appId": "careers",
    "scopes": ["careers2"],
    "query": {"bool": {"must": [{"simple_query_string": {
        "query": "hashicorp",
        "fields": ["keywords^1", "body^1", "url^2", "description^2",
                   "h1s_content^2", "title^3", "field_text_01"]}}]}},
    "aggs": {
        "field_keyword_172": {"filter": {"match_all": {}}, "aggs": {
            "field_keyword_17": {"terms": {"field": "field_keyword_17", "size": 6}},
            "field_keyword_17_count": {"cardinality": {"field": "field_keyword_17"}}}},
        "field_keyword_083": {"filter": {"match_all": {}}, "aggs": {
            "field_keyword_08": {"terms": {"field": "field_keyword_08", "size": 6}},
            "field_keyword_08_count": {"cardinality": {"field": "field_keyword_08"}}}},
        "field_keyword_184": {"filter": {"match_all": {}}, "aggs": {
            "field_keyword_18": {"terms": {"field": "field_keyword_18", "size": 6}},
            "field_keyword_18_count": {"cardinality": {"field": "field_keyword_18"}}}},
        "field_keyword_055": {"filter": {"match_all": {}}, "aggs": {
            "field_keyword_05": {"terms": {"field": "field_keyword_05", "size": 1000}},
            "field_keyword_05_count": {"cardinality": {"field": "field_keyword_05"}}}}},
    "size": SEARCH_PAGE_SIZE,
    "sort": [{"_score": "desc"}, {"pageviews": "desc"}],
    "lang": "zz",
    "localeSelector": {},
    "sm": {"query": "hashicorp", "lang": "zz"},
    "_source": ["_id", "title", "url", "description", "language", "entitled",
                "field_keyword_17", "field_keyword_08", "field_keyword_18",
                "field_keyword_19"],
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """The browser-reference normalization: NFC, collapse whitespace, trim."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def compact_body_text(value: str | None) -> str:
    """Whitespace-free NFC text used for rendered-vs-stored body comparison.

    IBM's JobDetail markup contains CSS-hidden ``<br>`` elements and inline
    labels whose browser innerText has no separating space, while an HTML text
    extraction may insert one.  Removing all whitespace compares the actual
    character content without hiding any word or punctuation difference.
    """
    return re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def title_slug(title: str | None) -> str:
    slug = re.sub(r"\s+", "-", str(title or "").strip())
    slug = re.sub(r"[^A-Za-z0-9\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "job"


def pretty_detail_url(title: str | None, job_id: str | int) -> str:
    if not re.fullmatch(r"\d+", str(job_id)):
        raise ValueError(f"HashiCorp/IBM exposed jobId is not numeric: {job_id!r}")
    return f"https://{DETAIL_HOST}/en_US/careers/JobDetail/{title_slug(title)}/{job_id}"


def _parse_job_id(search_url: str) -> str:
    parts = urlsplit(str(search_url))
    if parts.scheme != "https" or parts.hostname != DETAIL_HOST:
        raise ValueError(f"HashiCorp listing exposes a non-IBM detail URL: {search_url}")
    if parts.path.rstrip("/") != "/careers/JobDetail":
        raise ValueError(f"HashiCorp listing detail URL has an unexpected path: {search_url}")
    values = parse_qs(parts.query).get("jobId", [])
    if len(values) != 1 or not re.fullmatch(r"\d+", values[0]):
        raise ValueError(f"HashiCorp listing detail URL has no numeric jobId: {search_url}")
    return values[0]


def _search_body(from_offset: int = 0) -> dict:
    body = json.loads(json.dumps(_SEARCH_BODY))
    if from_offset:
        body["from"] = from_offset
    return body


def parse_official_entry(html: str, response_url: str = ENTRY_URL) -> dict:
    """Verify HashiCorp's own page publishes the exact IBM search we follow."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "www.hashicorp.com"
            or parts.path.rstrip("/") != "/en/careers"):
        raise ValueError("HashiCorp official careers entry is not the inspected /en/careers URL")
    soup = BeautifulSoup(html, "lxml")
    h1s = [normalized_title(h.get_text(" ", strip=True)) for h in soup.find_all("h1")]
    if "Careers at HashiCorp" not in h1s:
        raise ValueError("HashiCorp official careers page no longer has its 'Careers at HashiCorp' heading")
    page_text = normalized_title(soup.get_text(" ", strip=True))
    if "IBM" not in page_text:
        raise ValueError("HashiCorp official careers page no longer identifies itself as part of IBM")

    links, other_job_links = [], []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response_url, anchor["href"].strip())
        text = normalized_title(anchor.get_text(" ", strip=True))
        if "ibm.com/careers" in href:
            if href != SEARCH_ENTRY_URL:
                raise ValueError(f"HashiCorp publishes an unexpected IBM careers destination: {href}")
            links.append({"text": text, "href": href})
        elif (re.search(r"(?:job|career|position|opening)", text, re.I)
              and href.startswith("http")
              and urlsplit(href).hostname != "www.hashicorp.com"):
            other_job_links.append({"text": text, "href": href})
    if not links:
        raise ValueError("HashiCorp official careers page no longer links to its IBM open-positions search")
    if not any("open positions" in item["text"].lower() for item in links):
        raise ValueError("HashiCorp official careers page no longer labels its IBM link 'Open positions'")
    return {
        "url": response_url,
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "h1": h1s,
        "ibm_open_positions_links": links,
        "link_count": len(links),
        "other_job_like_links": other_job_links,
        "identifies_as_ibm_company": True,
    }


def parse_search_payload(payload: dict, requested_from: int = 0) -> dict:
    """Parse one IBM careers search response and verify its pagination shape."""
    if not isinstance(payload, dict):
        raise ValueError("HashiCorp/IBM search response is not an object")
    hits_block = payload.get("hits")
    if not isinstance(hits_block, dict):
        raise ValueError("HashiCorp/IBM search response has no hits object")
    hits = hits_block.get("hits")
    total = hits_block.get("total")
    if not isinstance(hits, list) or not isinstance(total, dict):
        raise ValueError("HashiCorp/IBM search response is missing hits or total")
    if total.get("relation") != "eq" or not isinstance(total.get("value"), int):
        raise ValueError("HashiCorp/IBM search response total is not exact")
    if len(hits) > SEARCH_PAGE_SIZE:
        raise ValueError("HashiCorp/IBM search page exceeds the observed page size")

    rows, seen, issues = [], set(), []
    expected_rows = min(SEARCH_PAGE_SIZE, max(0, total["value"] - requested_from))
    if len(hits) != expected_rows:
        issues.append(f"search_page_row_count_mismatch:{len(hits)}!={expected_rows}")
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
            raise ValueError("HashiCorp/IBM search hit lacks an object _source")
        source = hit["_source"]
        if not isinstance(source.get("title"), str) or not source.get("title").strip():
            raise ValueError("HashiCorp/IBM search hit lacks a title")
        if not isinstance(source.get("url"), str):
            raise ValueError("HashiCorp/IBM search hit lacks a url")
        job_id = _parse_job_id(source["url"])
        if job_id in seen:
            issues.append(f"search_duplicate_job_id:{job_id}")
            continue
        seen.add(job_id)
        if not isinstance(source.get("description"), str):
            issues.append(f"search_hit_description_not_string:{job_id}")
        if not source.get("field_keyword_08"):
            issues.append(f"search_hit_area_of_work_missing:{job_id}")
        if not source.get("field_keyword_19"):
            issues.append(f"search_hit_location_missing:{job_id}")
        rows.append({"job_id": job_id, "hit": hit, "source": source})
    if total["value"] < len(rows):
        issues.append("search_total_smaller_than_page_rows")
    return {
        "rows": rows,
        "ids": [row["job_id"] for row in rows],
        "total": total["value"],
        "from": requested_from,
        "page_size": SEARCH_PAGE_SIZE,
        "issues": issues,
    }


def _field_parts(field) -> tuple[str | None, str, str]:
    label_el = field.select_one(".article__content__view__field__label")
    value_el = field.select_one(".article__content__view__field__value")
    label = normalized_title(label_el.get_text(" ", strip=True)) if label_el else None
    if value_el is None:
        return label, "", ""
    value_html = value_el.decode_contents()
    return label, value_html, text_html(value_html).strip()


def _parse_date(value) -> str | None:
    """Normalize only dates actually published by the page; never guess."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", text)
    if match:
        return "-".join(match.groups())
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(value: str | None) -> float | None:
    if value is None:
        return None
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _job_id_from_login(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.hostname != DETAIL_HOST or not parts.path.rstrip("/").endswith("/careers/Login"):
        return None
    values = parse_qs(parts.query).get("jobId", [])
    return values[0] if len(values) == 1 and values[0].isdigit() else None


def parse_detail(html: str, job_id: str | int) -> dict:
    """Parse the full JobDetail widget, metadata article and JSON-LD JobPosting."""
    identity = str(job_id)
    if not re.fullmatch(r"\d+", identity):
        raise ValueError(f"HashiCorp/IBM detail jobId is not numeric: {job_id!r}")
    soup = BeautifulSoup(html, "lxml")
    widget = soup.find(id=WIDGET_ID)
    if widget is None:
        raise ValueError(f"IBM JobDetail {identity} has no #{WIDGET_ID} element")
    description_html = widget.decode_contents()
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"IBM JobDetail {identity} description is missing or implausibly short")

    sections, issues = [], []
    by_label = {}
    for field in widget.select(".article__content__view__field"):
        label, value_html, value_text = _field_parts(field)
        if not value_text:
            continue
        section = {"label": label, "text": value_text, "html": value_html}
        sections.append(section)
        if label:
            if label in by_label:
                issues.append(f"duplicate_section_label:{label}")
            else:
                by_label[label] = value_text
    unlabeled = [section["text"] for section in sections if not section["label"]]
    if not sections:
        raise ValueError(f"IBM JobDetail {identity} widget has no description sections")

    metadata_article = None
    for article in soup.find_all("article"):
        if "article--details" not in (article.get("class") or []):
            continue
        labels = [_field_parts(f)[0] for f in article.select(".article__content__view__field")]
        if "Job ID" in labels:
            metadata_article = article
            break
    if metadata_article is None:
        metadata_article = soup.select_one("article.article--sidebar")
    if metadata_article is None:
        raise ValueError(f"IBM JobDetail {identity} has no metadata article")
    metadata, metadata_html = {}, {}
    for field in metadata_article.select(".article__content__view__field"):
        label, value_html, value_text = _field_parts(field)
        if label and value_text:
            metadata[label] = value_text
            metadata_html[label] = value_html
    if metadata.get("Job ID") != identity:
        raise ValueError(f"IBM JobDetail {identity} metadata Job ID does not match the listing")

    jsonld_all = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            issues.append("jsonld_not_parseable")
            continue
        jsonld_all.append(data)
    jsonld = {}
    for data in jsonld_all:
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            jsonld = data
            break

    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    language = normalized_title(soup.html.get("lang")) if soup.html and soup.html.get("lang") else None
    h2_titles = [normalized_title(h.get_text(" ", strip=True)) for h in soup.find_all("h2")]
    h2_titles = [title for title in h2_titles if title]
    title_candidates = [
        ("metadata.Job Title", metadata.get("Job Title")),
        ("jsonld.title", jsonld.get("title")),
        ("page.h2", h2_titles[0] if h2_titles else None),
        ("page.title", re.sub(rf"\s+-\s+{re.escape(identity)}\s+-\s+IBM\s*$", "", page_title).strip() or None),
    ]
    detail_title = next((normalized_title(value) for _, value in title_candidates
                         if isinstance(value, str) and value.strip()), "")
    if not detail_title:
        issues.append("detail_title_missing")

    date_jsonld_raw = jsonld.get("datePosted")
    date_metadata_raw = metadata.get("Date posted")
    date_posted = _parse_date(date_jsonld_raw)
    if date_posted is None:
        date_posted = _parse_date(date_metadata_raw)
    if date_posted is None and (date_jsonld_raw or date_metadata_raw):
        issues.append("posting_date_not_parseable")

    minimum_raw = metadata.get("Projected Minimum Salary per year")
    maximum_raw = metadata.get("Projected Maximum Salary per year")
    salary = None
    if minimum_raw or maximum_raw:
        salary = {
            "minimum": _money(minimum_raw),
            "maximum": _money(maximum_raw),
            "raw_minimum": minimum_raw,
            "raw_maximum": maximum_raw,
            "period": "year",
            "currency": None,
            "currency_note": "The JobDetail page labels projected annual salary but does not state a currency.",
        }

    address = {}
    job_location = jsonld.get("jobLocation")
    if isinstance(job_location, dict) and isinstance(job_location.get("address"), dict):
        address = job_location["address"]
    locations = []
    location = {
        "city": metadata.get("City / Township / Village") or address.get("addressLocality"),
        "state": metadata.get("State / Province") or address.get("addressRegion"),
        "country": metadata.get("Country") or address.get("addressCountry"),
    }
    if any(location.values()):
        locations.append(location)

    apply_url = None
    for anchor in soup.find_all("a", href=True):
        href = urljoin("https://careers.ibm.com/", anchor["href"].strip())
        if _job_id_from_login(href) == identity:
            apply_url = href
            break
    if apply_url is None:
        issues.append("apply_login_link_missing")

    experience = None
    for text in unlabeled:
        match = re.match(r"Years of Experience\s*:\s*(.+)", text, re.I | re.S)
        if match:
            experience = normalized_title(match.group(1))
            break
    if experience is None and metadata.get("Years of Experience"):
        experience = metadata["Years of Experience"]

    return {
        "job_id": identity,
        "detail_title": detail_title,
        "title_candidates": [{"source": source, "value": value} for source, value in title_candidates],
        "page_title": page_title,
        "h2_titles": h2_titles,
        "sections": sections,
        "sections_by_label": by_label,
        "unlabeled_sections": unlabeled,
        "description_html": description_html,
        "description_text": description_text,
        "metadata": metadata,
        "metadata_html": metadata_html,
        "jsonld": jsonld,
        "jsonld_all": jsonld_all,
        "date_posted": date_posted,
        "date_posted_jsonld_raw": date_jsonld_raw,
        "date_posted_metadata_raw": date_metadata_raw,
        "salary": salary,
        "locations": locations,
        "apply_url": apply_url,
        "experience": experience,
        "language": language,
        "issues": issues,
    }


# Round 3 (user decision 2026-11-08) hiring-attribution header patterns.
# A header match requires an explicit phrase naming HashiCorp as the hiring
# business / team the role joins -- not merely a product name ("HashiCorp
# Terraform/Vault"), a skill, a certification, or a partner product team.
# Patterns are matched case-insensitively against the header text defined in
# classify_round3_attribution().  Each entry is (rule_name, compiled_regex).
HEADER_PATTERNS = [
    ("header_team_at_hashicorp", re.compile(r"team at hashicorp", re.I)),
    ("header_customers_at_hashicorp", re.compile(r"customers at (?:ibm )?hashicorp", re.I)),
    ("header_at_hashicorp_ibm_company", re.compile(r"at hashicorp, an ibm company", re.I)),
    ("header_contributing_to_hashicorp_offerings", re.compile(r"contributing to hashicorp's offerings", re.I)),
    ("header_supporting_hashicorp_portfolio", re.compile(r"supporting the hashicorp portfolio", re.I)),
    ("header_at_ibm_hashicorp", re.compile(r"at (?:ibm )?hashicorp\b", re.I)),
    ("header_hashicorp_portfolio_role", re.compile(r"hashicorp portfolio\)", re.I)),
    ("header_build_operate_hashicorp_vault", re.compile(r"design, build, and operate hashicorp vault", re.I)),
    ("header_selling_hashicorp", re.compile(r"selling hashicorp", re.I)),
    ("header_hashicorp_automation_solutions", re.compile(r"hashicorp automation solutions", re.I)),
]
ROUND3_STRUCTURED_FIELDS = (
    "Company", "Area of work", "Position type", "Employment type",
    "Contract type", "Work arrangement", "Shift",
)


def _header_text(sections) -> tuple[str, str, str]:
    """Return (introduction, responsibilities_opening, combined) for round 3.

    ``sections`` may be the ``sections_by_label`` dict from ``parse_detail``
    or the full ``sections`` list of ``{label, text}`` dicts.  The
    responsibilities opening is the first 800 characters of the
    ``Your role and responsibilities`` section (its opening paragraph,
    before duty lists), which is where several roles state the hiring
    business when the Introduction is generic IBM boilerplate.
    """
    if isinstance(sections, dict):
        intro = str(sections.get("Introduction") or "")
        resp = str(sections.get("Your role and responsibilities") or "")
    else:
        intro, resp = "", ""
        for section in sections or []:
            label = section.get("label") if isinstance(section, dict) else None
            text = str(section.get("text") or "") if isinstance(section, dict) else ""
            if label == "Introduction" and text:
                intro = text
            elif label == "Your role and responsibilities" and text:
                resp = text
    resp_opening = normalized_body_text(resp)[:800]
    intro_norm = normalized_body_text(intro)
    return intro_norm, resp_opening, normalized_body_text(f"{intro_norm} {resp_opening}")


def classify_round3_attribution(*, search_title=None, detail_title=None,
                               metadata=None, search_source=None,
                               sections=None) -> dict:
    """Apply the round 3 user scope rule to one parent-board posting.

    Returns ``{included, rules, primary_rule, evidence, ...}`` where
    ``rules`` lists every matching rule among:

    - ``structured_field``: a structured field value (legal employer
      ``Company`` metadata first, then other metadata/search facet fields)
      explicitly names HashiCorp.  The IBM board exposes no
      company/business-unit facet and every legal employer in the
      published search is an IBM entity, so this rule matches 0 of the
      23 published postings (verified 2026-11-08).
    - ``title_listing`` / ``title_detail``: the listing/search title or
      the detail ``Job Title`` explicitly contains ``HashiCorp``.
    - ``description_header``: the role's own description header
      (Introduction, or the opening of Your role and responsibilities)
      contains an explicit hiring-attribution phrase (HEADER_PATTERNS).

    A posting is included iff at least one rule matches.  Mentions of
    HashiCorp products as a skill/certification/partner product in
    Required/Preferred sections, or in the body outside the header,
    do not match any rule and are therefore excluded.
    """
    metadata = dict(metadata or {})
    rules, evidence = [], {}
    # (a) Structured fields: legal employer first, then other structured
    # metadata and search facet/source fields.
    structured_values = {}
    for field in ROUND3_STRUCTURED_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            structured_values[f"metadata:{field}"] = value
    if isinstance(search_source, dict):
        for field in ("field_keyword_08", "field_keyword_17", "field_keyword_18",
                      "field_keyword_19", "title"):
            value = search_source.get(field)
            if isinstance(value, str) and value.strip():
                structured_values[f"search:{field}"] = value
    for field, value in structured_values.items():
        if re.search(r"hashicorp", value, re.I) and field != "search:title":
            rules.append("structured_field")
            evidence["structured_field"] = {"field": field, "value": value}
            break
    # (b) Explicit company name in the job title.
    if isinstance(search_title, str) and re.search(r"hashicorp", search_title, re.I):
        rules.append("title_listing")
        evidence["title_listing"] = {"field": "search_title", "value": search_title}
    if isinstance(detail_title, str) and re.search(r"hashicorp", detail_title, re.I):
        rules.append("title_detail")
        evidence["title_detail"] = {"field": "detail_title", "value": detail_title}
    # (c) Description header naming HashiCorp as the hiring business.
    intro, resp_opening, header = _header_text(sections)
    for name, pattern in HEADER_PATTERNS:
        match = pattern.search(header)
        if match:
            start = max(0, match.start() - 160)
            rules.append("description_header")
            evidence["description_header"] = {
                "pattern": name,
                "quote": header[start:match.end() + 160].strip(),
                "in_introduction": bool(pattern.search(intro)),
                "in_responsibilities_opening": bool(pattern.search(resp_opening)),
            }
            break
    primary = None
    for candidate in ("structured_field", "title_listing", "title_detail",
                      "description_header"):
        if candidate in rules:
            primary = candidate
            break
    return {
        "included": bool(rules),
        "rules": rules,
        "primary_rule": primary,
        "evidence": evidence,
        "structured_field_values": structured_values,
        "header_preview": header[:800],
    }


class Hashicorp(Adapter):
    """Follow HashiCorp's published IBM search and exact JobDetail pages."""

    VERIFICATION_REVISION = "hashicorp.ibm-careers-search-and-jobdetail.round3.2"
    SEARCH_API_URL = SEARCH_API_URL

    def __init__(self, http, company, source, options, detail_fetcher=None):
        super().__init__(http, company, source, options)
        # async callable (job_id, title) -> (html_text, final_url, http_status).
        # Defaults to a WAF-warmed Playwright session; tests inject fixtures.
        self.detail_fetcher = detail_fetcher
        self._browser = None

    # -- browser session ---------------------------------------------------
    async def _ensure_browser(self):
        """One shared Chromium session for the Vercel-checkpoint entry page and
        the AWS-WAF detail pages.  Never more than one page is open."""
        if self._browser is None:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                locale="en-US",
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()
            self._browser = (pw, browser, page)
        return self._browser

    async def _browser_goto(self, url: str, *, settle_ms: int = 0):
        """Navigate with the real browser and return HTML, final URL, status
        and the full redirect chain (the HashiCorp Vercel checkpoint 429s
        non-browser clients; no CAPTCHA is solved)."""
        _, _, page = await self._ensure_browser()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        status = response.status if response is not None else None
        if settle_ms:
            await page.wait_for_timeout(settle_ms)
        html = await page.content()
        chain, request = [], response.request if response is not None else None
        while request is not None:
            item = await request.response()
            chain.append({
                "status": item.status if item is not None else None,
                "url": request.url,
                "location": item.headers.get("location") if item is not None else None,
            })
            request = request.redirected_from
        return html, page.url, status, chain

    # -- detail fetching ---------------------------------------------------
    async def _browser_fetch(self, job_id: str, title: str):
        """Fetch one pretty JobDetail page in the WAF-warmed browser session."""
        url = pretty_detail_url(title, job_id)
        if self._browser is None or not getattr(self, "_warmed", False):
            await self._ensure_browser()
            _, _, page = self._browser
            # Warm up: the query form settles the AWS WAF challenge and sets
            # the browser cookies.  It intentionally is not used as a detail
            # page because its widget is empty.
            await page.goto(DETAIL_QUERY_URL.format(job_id=job_id),
                            wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            self._warmed = True

        _, _, page = self._browser
        last_error = None
        for attempt in range(3):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                status = response.status if response is not None else None
                if status == 429:
                    raise ValueError(f"JobDetail {job_id} returned HTTP 429; stopping")
                if status != 200:
                    last_error = ValueError(f"JobDetail {job_id} returned HTTP {status}")
                    await page.wait_for_timeout(3000)
                    continue
                try:
                    await page.wait_for_function(
                        "() => { const el = document.getElementById('jobDetailWidget'); "
                        "return el && el.innerText && el.innerText.length > 200; }",
                        timeout=30000,
                    )
                except Exception:
                    pass
                html = await page.content()
                final_url = page.url
                if f'id="{WIDGET_ID}"' not in html:
                    last_error = ValueError(f"JobDetail {job_id} page has no #{WIDGET_ID} widget")
                    await page.wait_for_timeout(3000)
                    continue
                return html, final_url, status
            except ValueError:
                raise
            except Exception as exc:
                last_error = exc
                await page.wait_for_timeout(3000)
        raise last_error or ValueError(f"JobDetail {job_id} fetch failed")

    async def _close_browser(self):
        if self._browser is not None:
            pw, browser, _ = self._browser
            self._browser = None
            try:
                await browser.close()
            finally:
                await pw.stop()

    # -- archival (browser details share the HTTP archive schema) ---------
    def _archive(self, *, url: str, final_url: str, status: int, content: bytes,
                 source_kind: str = "exact_exposed_job_detail"):
        output = getattr(self.http, "output", None)
        if not output:
            return None
        output = Path(output)
        (output / "responses").mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        target = output / "responses" / f"{digest}.gz"
        if not target.exists():
            target.write_bytes(gzip.compress(content))
        record = {
            "url": url,
            "final_url": final_url,
            "method": "GET",
            "request_body": None,
            "status": status,
            "sha256": digest,
            "content_type": "text/html",
            "redirects": [],
            "fetched_at": now(),
            "fetcher": "playwright-chromium",
            "source_kind": source_kind,
        }
        with (output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    # -- job assembly ------------------------------------------------------
    def _make_job(self, hit, detail_html: str, detail_url: str, detail_status: int):
        source = hit["_source"]
        job_id = _parse_job_id(source["url"])
        parsed = parse_detail(detail_html, job_id)
        metadata = parsed["metadata"]
        sections = parsed["sections_by_label"]
        raw = {
            "search": source,
            "search_hit_id": hit.get("_id"),
            "search_hit_index": hit.get("_index"),
            "search_hit_sort": hit.get("sort"),
            "search_score": hit.get("_score"),
            "search_title": source.get("title"),
            "search_location": source.get("field_keyword_19"),
            "search_description_snippet": source.get("description"),
            "detail_url": detail_url,
            "detail_status": detail_status,
            "detail_title": parsed["detail_title"],
            "title_candidates": parsed["title_candidates"],
            "detail_page_title": parsed["page_title"],
            "detail_sections": parsed["sections"],
            "detail_unlabeled_sections": parsed["unlabeled_sections"],
            "detail_metadata": metadata,
            "detail_jsonld": parsed["jsonld"],
            "detail_issues": parsed["issues"],
            "description_normalized_chars": len(normalized_body_text(parsed["description_text"])),
            "description_fnv1a64": fnv1a64(normalized_body_text(parsed["description_text"])),
            "description_compact_chars": len(compact_body_text(parsed["description_text"])),
            "description_compact_fnv1a64": fnv1a64(compact_body_text(parsed["description_text"])),
            "round3_attribution": classify_round3_attribution(
                search_title=source.get("title"),
                detail_title=parsed["detail_title"],
                metadata=metadata,
                search_source=source,
                sections=parsed["sections"],
            ),
        }
        job = self.job(
            raw,
            title=parsed["detail_title"] or source["title"],
            url=detail_url,
            job_id=job_id,
            requisition_id=None,
            apply_url=parsed["apply_url"],
            description=parsed["description_html"],
            method="hashicorp.ibm_careers_jobdetail.pretty_url.widget",
            description_url=detail_url,
            locations=parsed["locations"],
            department=metadata.get("Area of work") or source.get("field_keyword_08"),
            team=None,
            employment_type=metadata.get("Employment type") or metadata.get("Contract type"),
            workplace_type=metadata.get("Work arrangement") or source.get("field_keyword_17"),
            date_posted=parsed["date_posted"],
            date_posted_raw={
                "jsonld": parsed["date_posted_jsonld_raw"],
                "metadata": parsed["date_posted_metadata_raw"],
            },
            date_updated=None,
            valid_through=None,
            salary=parsed["salary"],
            qualifications=sections.get("Required technical and professional expertise"),
            responsibilities=sections.get("Your role and responsibilities"),
            experience=parsed["experience"],
            education={
                "required": sections.get("Required education"),
                "preferred": sections.get("Preferred education"),
            } if (sections.get("Required education") or sections.get("Preferred education")) else None,
            employer=metadata.get("Company"),
            language=parsed.get("language"),
        )
        job.description_text = parsed["description_text"]
        job.record_identity = f"hashicorp:ibm-job:{job_id}"
        return job

    def _unavailable_job(self, hit, reason: str):
        source = hit["_source"]
        job_id = _parse_job_id(source["url"])
        # Without the detail body only the title rules can be applied; a
        # header-only posting whose detail failed stays unclassified (the
        # caller records it as a failure, never as an exclusion).
        _title_attribution = classify_round3_attribution(
            search_title=source.get("title"), search_source=source, sections=[])
        raw = {"round3_attribution_title_only": _title_attribution,
            "search": source,
            "search_hit_id": hit.get("_id"),
            "search_hit_index": hit.get("_index"),
            "search_hit_sort": hit.get("sort"),
            "search_score": hit.get("_score"),
            "search_title": source.get("title"),
            "search_location": source.get("field_keyword_19"),
            "search_description_snippet": source.get("description"),
            "detail_fetch_failed": True,
            "detail_failure_reason": reason,
        }
        job = self.job(
            raw,
            title=source.get("title"),
            url=pretty_detail_url(source.get("title"), job_id),
            job_id=job_id,
            requisition_id=None,
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=[{"search_location": source.get("field_keyword_19")}],
            department=source.get("field_keyword_08"),
        )
        job.description_unavailable = True
        job.record_identity = f"hashicorp:ibm-job:{job_id}"
        job.warnings.append("detail_fetch_failed")
        return job

    # -- run ---------------------------------------------------------------
    async def run(self):
        try:
            return await self._run_impl()
        finally:
            await self._close_browser()

    async def _run_impl(self):
        started = self.result._started_clock
        if self.company.careers_url.rstrip("/") != ENTRY_URL:
            raise ValueError(f"HashiCorp dedicated flow must start from {ENTRY_URL}")

        listing_started = time.perf_counter()
        if not self.page_guard():
            return self.result
        entry_html, entry_final_url, entry_status, entry_chain = await self._browser_goto(
            ENTRY_URL, settle_ms=1500)
        if entry_status != 200 or entry_final_url.rstrip("/") != ENTRY_URL:
            raise ValueError(
                f"HashiCorp official careers entry failed: HTTP {entry_status} -> {entry_final_url}")
        official = parse_official_entry(entry_html, entry_final_url)
        self._archive(url=ENTRY_URL, final_url=entry_final_url, status=entry_status,
                      content=entry_html.encode("utf-8"), source_kind="official_entry")

        if not self.page_guard():
            return self.result
        redirect_html, redirect_final_url, redirect_status, redirect_history = await self._browser_goto(
            OPEN_POSITIONS_URL, settle_ms=2500)
        if redirect_final_url != SEARCH_ENTRY_URL:
            raise ValueError(
                f"HashiCorp open-positions route no longer lands on the inspected IBM search: "
                f"{redirect_final_url}")
        if redirect_status != 200:
            raise ValueError(f"HashiCorp open-positions final page returned HTTP {redirect_status}")
        if not any(item["status"] == 308 and item["location"] == SEARCH_ENTRY_URL
                   for item in redirect_history):
            self.result.warnings.append("open_positions_308_history_not_observed")
        self._archive(url=OPEN_POSITIONS_URL, final_url=redirect_final_url,
                      status=redirect_status, content=redirect_html.encode("utf-8"),
                      source_kind="official_open_positions_redirect")
        if ("career-search" not in redirect_html or "careers2" not in redirect_html
                or "appid: 'careers'" not in redirect_html):
            raise ValueError("IBM careers search shell no longer exposes the inspected search component")

        pages, by_id, ordered_ids = [], {}, []
        expected_total = None
        offset = 0
        while True:
            if not self.page_guard():
                self.result.errors.append("page_budget_exhausted_during_listing")
                break
            payload = await self.http.json(
                SEARCH_API_URL, method="POST", json=_search_body(offset))
            page = parse_search_payload(payload, offset)
            pages.append({"from": offset, "hits": len(page["rows"]),
                          "total": page["total"], "ids": page["ids"],
                          "issues": page["issues"]})
            if expected_total is None:
                expected_total = page["total"]
            elif page["total"] != expected_total:
                self.result.errors.append(
                    f"search_total_changed_between_pages: {expected_total} -> {page['total']}")
            for row in page["rows"]:
                if row["job_id"] not in by_id:
                    by_id[row["job_id"]] = row["hit"]
                    ordered_ids.append(row["job_id"])
            if len(ordered_ids) >= expected_total or not page["rows"]:
                break
            offset += SEARCH_PAGE_SIZE
            if offset > 10000:
                self.result.errors.append("search_pagination_runaway")
                break

        # One empty page beyond the last full page proves there is no hidden
        # numeric pagination page.  The UI displays all 23 in one page.
        if expected_total is not None:
            next_offset = ((expected_total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE) * SEARCH_PAGE_SIZE
            if all(item["from"] != next_offset for item in pages):
                if not self.page_guard():
                    self.result.errors.append("page_budget_exhausted_during_listing")
                else:
                    payload = await self.http.json(
                        SEARCH_API_URL, method="POST", json=_search_body(next_offset))
                    page = parse_search_payload(payload, next_offset)
                    pages.append({"from": next_offset, "hits": len(page["rows"]),
                                  "total": page["total"], "ids": page["ids"],
                                  "issues": page["issues"]})
                    if page["rows"]:
                        self.result.errors.append(
                            f"search_exposes_unexpected_rows_beyond_total:{len(page['rows'])}")
        listing_seconds = time.perf_counter() - listing_started
        if expected_total is None or len(ordered_ids) != expected_total:
            self.result.errors.append(
                f"search_pagination_reconciliation_failed: unique={len(ordered_ids)} "
                f"expected={expected_total}")
        self.result.expected_total = expected_total

        # Details (browser, WAF-warmed).  Fetch, parse and add are interleaved
        # so a crash mid-run preserves finished jobs via the checkpoint hook.
        detail_seconds = 0.0
        parse_seconds = 0.0
        failed_detail_ids = []
        excluded = []
        unclassified_failed = []
        rule_counts = {"structured_field": 0, "title_listing": 0,
                       "title_detail": 0, "description_header": 0}
        included_ids = []
        fetcher = self.detail_fetcher or self._browser_fetch

        def _handle_failure(job_id, hit, reason):
            # A failed detail can still be title-classified; only a
            # title-included failure is exported (as unavailable).  Any
            # other failure stays unclassified -- never silently excluded.
            title_only = classify_round3_attribution(
                search_title=hit["_source"].get("title"),
                search_source=hit["_source"], sections=[])
            if title_only["included"]:
                self.add(self._unavailable_job(hit, reason))
                included_ids.append(job_id)
            else:
                unclassified_failed.append({"id": job_id, "reason": reason,
                                          "title_only_attribution": title_only})

        try:
            for job_id in ordered_ids:
                hit = by_id[job_id]
                title = hit["_source"]["title"]
                request_started = time.perf_counter()
                try:
                    html, final_url, status = await fetcher(job_id, title)
                except Exception as exc:
                    detail_seconds += time.perf_counter() - request_started
                    failed_detail_ids.append(job_id)
                    reason = f"{type(exc).__name__}: {exc}"
                    self.result.errors.append(f"JobDetail {job_id} ({title}): {reason}")
                    _handle_failure(job_id, hit, reason)
                    continue
                detail_seconds += time.perf_counter() - request_started
                if status != 200:
                    failed_detail_ids.append(job_id)
                    reason = f"HTTP {status}"
                    self.result.errors.append(f"JobDetail {job_id} returned HTTP {status}")
                    _handle_failure(job_id, hit, reason)
                    continue
                self._archive(url=pretty_detail_url(title, job_id), final_url=final_url,
                              status=status, content=html.encode("utf-8"))
                parse_one = time.perf_counter()
                try:
                    job = self._make_job(hit, html, final_url, status)
                except Exception as exc:
                    parse_seconds += time.perf_counter() - parse_one
                    failed_detail_ids.append(job_id)
                    reason = f"{type(exc).__name__}: {exc}"
                    self.result.errors.append(f"JobDetail {job_id} parse: {reason}")
                    _handle_failure(job_id, hit, reason)
                    continue
                parse_seconds += time.perf_counter() - parse_one
                attribution = job.raw.get("round3_attribution") or {}
                if not attribution.get("included"):
                    excluded.append({
                        "id": job_id,
                        "search_title": hit["_source"].get("title"),
                        "detail_title": job.raw.get("detail_title"),
                        "url": job.url,
                        "rules": attribution.get("rules", []),
                        "reason": ("no structured HashiCorp field, no HashiCorp in "
                                   "listing/detail title, and no hiring-business "
                                   "phrase in the description header; HashiCorp "
                                   "mentions are product/skill-only"),
                    })
                    continue
                for rule in attribution.get("rules", []):
                    if rule in rule_counts:
                        rule_counts[rule] += 1
                included_ids.append(job_id)
                self.add(job)
        finally:
            await self._close_browser()

        # Final listing recheck: the live IBM index can change during a run.
        final_reconciliation = {}
        try:
            final_payload = await self.http.json(
                SEARCH_API_URL, method="POST", json=_search_body(0))
            final_page = parse_search_payload(final_payload, 0)
            final_ids = final_page["ids"]
            same_membership = set(final_ids) == set(ordered_ids)
            final_reconciliation = {
                "search_total": final_page["total"],
                "final_jobIds": final_ids,
                "initial_jobIds": ordered_ids,
                "same_membership": same_membership,
                "order_changed": final_ids != ordered_ids,
                "new_ids": [value for value in final_ids if value not in by_id],
                "missing_ids": [value for value in ordered_ids if value not in final_ids],
            }
            if not same_membership:
                self.result.errors.append(
                    "listing_membership_changed_during_run: "
                    f"initial={len(ordered_ids)} final={len(final_ids)} "
                    f"new={final_reconciliation['new_ids']} "
                    f"missing={final_reconciliation['missing_ids']}")
        except Exception as exc:
            final_reconciliation = {"error": f"{type(exc).__name__}: {exc}"}
            self.result.errors.append(f"final_listing_recheck_failed: {type(exc).__name__}: {exc}")

        # Round 3: expected_total is the included (HashiCorp-attributed)
        # count; the parent search total is retained in metadata so that
        # search_total == included + excluded + unclassified_failed.
        if not failed_detail_ids and expected_total is not None:
            self.result.expected_total = len(included_ids)
        self.result.listing_complete = (
            not self.result.errors
            and not unclassified_failed
            and expected_total is not None
            and len(by_id) == expected_total
            and len(included_ids) + len(excluded) == len(ordered_ids)
            and len(self.result.jobs) == len(included_ids)
            and all(job.description_verified for job in self.result.jobs))
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_browser_seconds"] = round(detail_seconds, 6)
        self.result.timings["detail_parse_seconds"] = round(parse_seconds, 6)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": ENTRY_URL,
            "ledger_entry_url": LEDGER_ENTRY_URL,
            "open_positions_url": OPEN_POSITIONS_URL,
            "search_entry_url": SEARCH_ENTRY_URL,
            "search_api_url": SEARCH_API_URL,
            "official_entry": official,
            "open_positions_redirect_history": redirect_history,
            "official_entry_transport": (
                "playwright-chromium; HashiCorp's Vercel Security Checkpoint "
                "returns HTTP 429 with x-vercel-mitigated=challenge to plain "
                "HTTP clients. A real browser is used for the public page; no "
                "CAPTCHA was solved and no challenge token was forged."),
            "membership_authority": (
                "Round 3 user decision 2026-11-08: HashiCorp was absorbed into "
                "IBM. HashiCorp's own careers page publishes six 'Open "
                "positions' links to IBM's careers search q=hashicorp, which is "
                "the enumeration vehicle (the IBM board exposes no "
                "company/business-unit facet or legal-employer value naming "
                "HashiCorp). Every jobId that search returns is fetched and "
                "classified under the round 3 rule in "
                "classify_round3_attribution(); only HashiCorp-attributed "
                "postings are exported."),
            "membership_note": (
                "Round 3 rule: include iff (a) a structured field (legal "
                "employer Company metadata, or a search facet/source field) "
                "names HashiCorp, or (b) HashiCorp appears in the job title "
                "(listing/search title or detail Job Title), or (c) the "
                "description header (Introduction, or the opening of Your "
                "role and responsibilities) names HashiCorp as the hiring "
                "business via an explicit hiring-attribution phrase. "
                "Product/skill/certification-only mentions are excluded."),
            "round3_rule": {
                "user_decision": "Round 3, 2026-11-08: collect only parent-board postings explicitly attributed to HashiCorp",
                "rule": "structured_field OR title_listing/title_detail OR description_header (see classify_round3_attribution)",
                "structured_fields_checked": list(ROUND3_STRUCTURED_FIELDS) + ["search field_keyword_08/17/18/19"],
                "structured_field_values_observed": "all legal employers are IBM entities; no structured value names HashiCorp; parent board facets are only work arrangement, area of work, experience level and location -- no company/business-unit facet",
                "rule_counts": rule_counts,
                "included_jobIds": included_ids,
                "excluded_jobIds": [item["id"] for item in excluded],
                "excluded": excluded,
                "unclassified_failed": unclassified_failed,
                "reconciliation": f"search_total({expected_total}) == included({len(included_ids)}) + excluded({len(excluded)}) + unclassified_failed({len(unclassified_failed)})",
            },
            "listing_filter_behavior": (
                "unfiltered search; facet aggregations (Remote/Hybrid, Career "
                "Areas, Experience level, Location) are reporting-only and no "
                "facet was applied"),
            "ui_pagination": (
                "IBM UI shows '1 - 23 of 23 items' with 10/20/30 page-size "
                "options and one page; API from=30 returns zero hits. The "
                "adapter paginates by 30 and verifies the empty next page."),
            "search_total": expected_total,
            "search_pages": pages,
            "search_jobIds_in_order": ordered_ids,
            "final_listing_reconciliation": final_reconciliation,
            "detail_failed_jobIds": failed_detail_ids,
            "detail_url_pattern": (
                "https://careers.ibm.com/en_US/careers/JobDetail/<slug>/<jobId> "
                "(slug ignored by server; jobId is authoritative)"),
            "description_source": (
                "exact exposed job's pretty JobDetail #jobDetailWidget inner "
                "HTML, including all labeled role sections and the widget's "
                "unlabeled IBM business-unit/boilerplate blocks; the search "
                "`description` is a truncated snippet and is never used as the "
                "description"),
            "application_links": (
                "each page's Apply anchor (Login?jobId=<id>) retained as "
                "apply_url; no login or application form was opened"),
            "dates": (
                "datePosted from the JobDetail JSON-LD JobPosting, cross-checked "
                "with the metadata 'Date posted' field; no updated dates, "
                "validThrough, or template dates are invented"),
            "salary": (
                "Projected Minimum/Maximum Salary per year metadata retained "
                "with exact raw strings; the page does not state a currency, so "
                "currency is null and no conversion is attempted"),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "listing_requests": len(pages),
            "detail_requests": len(ordered_ids),
            "detail_concurrency": 1,
            "detail_concurrency_note": (
                "WAF-warmed browser uses one page sequentially; detail_concurrency "
                "is intentionally 1 to respect the single-page/browser budget"),
        })
        self.result.timings["source_snapshot_phase_seconds"] = round(
            time.perf_counter() - started, 6)
        return self.result
