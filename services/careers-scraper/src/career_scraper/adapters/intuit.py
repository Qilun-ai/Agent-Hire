"""Intuit's own TalentBrew careers search plus its exact native job pages.

Membership authority is the official unfiltered search widget at
``https://jobs.intuit.com/search-jobs`` (tenant 27595). The page is a Radancy
TalentBrew site: the server-rendered listing publishes 15 cards per ``?p=N``
page, and the page's own search module requests the same results from
``GET /search-jobs/results``. Both routes are reconciled here; the coherent
single-response search snapshot (``RecordsPerPage`` at the live total) is the
membership authority, and a ``RecordsPerPage=15`` walk of every page checks it.

Full descriptions come from ``#job-team`` on each exact published
``/job/{city}/{slug}/27595/{url_job_id}`` detail URL, corroborated against the
page's JSON-LD ``JobPosting.description``. The visible per-job apply link goes
to ``intuit.avature.net`` (standard postings) or Intuit's own
``www.intuit.com/expert-careers/`` campaign page (seasonal tax-expert
postings); it is retained as the job's apply URL only and no ATS board is
enumerated. A company-wide "Company Overview" template block is retained raw
but is not role description.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ORG_ID = "27595"
BASE = "https://jobs.intuit.com"
ENTRY_URL = f"{BASE}/search-jobs"
RESULTS_URL = f"{BASE}/search-jobs/results"
CAREERS_HOST = "jobs.intuit.com"
APPLY_HOSTS = {"intuit.avature.net", "www.intuit.com", "talenteq.intuit.com"}
DETAIL_ROUTE = re.compile(r"^/job/[^/]+/[^/]+/27595/(\d+)/?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Exact unfiltered criteria sent by the site's TalentBrew search module. The
# module fills these from #search-results data attributes; with no filters the
# keyword/location/facet values are empty and only the page/size change.
SEARCH_CRITERIA = {
    "ActiveFacetID": 0,
    "Distance": 50,
    "RadiusUnitType": 0,
    "Keywords": "",
    "Location": "",
    "ShowRadius": "False",
    "IsPagination": "False",
    "FacetTerm": "",
    "FacetType": 0,
    "SearchResultsModuleName": "Search Results",
    "SearchFiltersModuleName": "Search Filters",
    "SortCriteria": 0,
    "SortDirection": 0,
    "SearchType": 5,
    "ResultsType": 0,
    "fc": "",
    "fl": "",
    "fcf": "",
    "afc": "",
    "afl": "",
    "afcf": "",
}

HEADING_RE = re.compile(
    r"^(?:job overview|about (?:the )?role|about you|about us|the opportunity|the mission|"
    r"responsibilities|qualifications|requirements|minimum (?:qualifications|requirements)|"
    r"preferred (?:qualifications|requirements)|what you(?:'|’)ll (?:do|need|bring)|"
    r"how you(?:'|’)ll (?:work|lead)|benefits|what we offer|why intuit|compensation)$",
    re.I,
)
PAY_HEADING = "The expected base pay range for this position is:"
QUOTE_TABLE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u2035": "'", "\u201c": '"', "\u201d": '"',
    "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u2036": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ",
})


def search_params(*, page: int = 1, per_page: int = 15) -> dict:
    """The observed unfiltered search-module query for one result page."""
    return {**SEARCH_CRITERIA, "CurrentPage": page, "RecordsPerPage": per_page}


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _canonical(value: str | None) -> str:
    """Whitespace- and quote-style-insensitive text used for corroboration.

    The visible body and the JSON-LD copy on this site differ by the leading
    "Job Overview" heading and by straight/curly quote normalisation (for
    example ``"Self-starter"`` vs ``'Self-starter'``). Nothing else differs.
    """
    value = _norm(value).translate(QUOTE_TABLE)
    return re.sub(r"[\"'`\u00b4]", "", value)


def _similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _parse_card(anchor, *, request_url: str) -> dict:
    href = anchor.get("href", "")
    match = DETAIL_ROUTE.match(urlsplit(href).path)
    card_job_id = anchor.get("data-job-id") or ""
    if not href or not match:
        raise ValueError(f"Intuit search card has an unreadable route: {href!r}")
    title_el = anchor.select_one("h2")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    if not title:
        raise ValueError(f"Intuit search card {card_job_id!r} has no title")
    card = anchor.find_parent("li") or anchor
    location_el = anchor.select_one(".job-location")
    return {
        "card_job_id": card_job_id,
        "url_job_id": match.group(1),
        "href": href,
        "url": urljoin(BASE + "/", href),
        "title": title,
        "card_title_attr": anchor.get("data-title"),
        "location_label": location_el.get_text(" ", strip=True) if location_el else "",
        "category": card.get("data-category"),
        "category_id": card.get("data-category-id"),
        "remote_attr": card.get("data-remote"),
        "original_location": card.get("data-orig-location"),
        "request_url": request_url,
    }


def parse_results_fragment(fragment: str, *, request_url: str) -> tuple[list[dict], dict]:
    """Parse the search widget's result fragment (wrapper meta + cards)."""
    soup = BeautifulSoup(fragment, "lxml")
    wrapper = soup.select_one("#search-results")
    if wrapper is None:
        raise ValueError(f"Intuit search fragment has no #search-results: {request_url}")
    try:
        total = int(wrapper.get("data-total-results"))
        total_pages = int(wrapper.get("data-total-pages"))
        current = int(wrapper.get("data-current-page"))
        per_page = int(wrapper.get("data-records-per-page"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Intuit search fragment lacks pagination attributes: {request_url}") from exc
    rows = [_parse_card(anchor, request_url=request_url)
            for anchor in soup.select("#search-results-list a.sr-item")]
    return rows, {"total": total, "total_pages": total_pages, "current": current, "per_page": per_page}


def parse_listing_page(html: str, *, page_url: str) -> tuple[list[dict], dict]:
    """Parse a server-rendered ``/search-jobs?p=N`` page (15 cards per page)."""
    soup = BeautifulSoup(html, "lxml")
    wrapper = soup.select_one("#search-results")
    if wrapper is None:
        raise ValueError(f"Intuit listing {page_url} has no #search-results")
    try:
        total = int(wrapper.get("data-total-results"))
        total_pages = int(wrapper.get("data-total-pages"))
        current = int(wrapper.get("data-current-page"))
        per_page = int(wrapper.get("data-records-per-page"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Intuit listing {page_url} lacks pagination attributes") from exc
    rows = [_parse_card(anchor, request_url=page_url)
            for anchor in soup.select("#search-results-list a.sr-item")]
    h1 = wrapper.select_one("h1")
    label = soup.select_one(".pagination-current-label")
    return rows, {
        "total": total,
        "total_pages": total_pages,
        "current": current,
        "per_page": per_page,
        "h1": h1.get_text(" ", strip=True) if h1 else None,
        "pager_label": label.get_text(" ", strip=True) if label else None,
    }


JOB_ID_TOKEN = re.compile(r"(\d{4}-\d{4,}|\d{4,})\s*$")


def _job_id_value(element) -> str | None:
    """Extract the published Job ID, including the French "N° de l'offre" label."""
    if element is None:
        return None
    text = _norm(element.get_text(" ", strip=True))
    match = JOB_ID_TOKEN.search(text)
    if match:
        return match.group(1)
    label = element.find("b")
    if label is not None:
        text = _norm(text.replace(_norm(label.get_text(" ", strip=True)), "", 1))
    return text or None


def _label_value(element, label: str) -> str | None:
    if element is None:
        return None
    text = _norm(element.get_text(" ", strip=True))
    text = re.sub(rf"^{re.escape(label)}\s*", "", text, flags=re.I).strip()
    return text or None


def _visible_location_text(element) -> str | None:
    """The visible location text, excluding the hidden campaign-location modal."""
    if element is None:
        return None
    clone = BeautifulSoup(str(element), "lxml")
    for popup in clone.select(".popup-locations"):
        popup.decompose()
    text = _norm(clone.get_text(" ", strip=True))
    text = re.sub(r"^Location\s*", "", text, flags=re.I).strip()
    return text or None


def _popup_locations(element) -> list[str]:
    if element is None:
        return []
    popup = element.select_one(".popup-locations")
    if popup is None:
        return []
    values = []
    for line in popup.get_text("\n", strip=True).splitlines():
        value = _norm(line)
        if value:
            values.append(value)
    return values


def _parse_info(soup: BeautifulSoup) -> tuple[dict, str]:
    """Return (job-info fields, observed template name)."""
    info: dict = {}
    template = "standard"
    standard = soup.select_one(".job-description__job-info")
    if standard is not None:
        info["category"] = _label_value(standard.select_one(".job-category-jd"), "Category")
        location_el = standard.select_one(".job-location-jd")
        info["job_id"] = _job_id_value(standard.select_one(".job-id"))
    else:
        template = "ajd"
        ajd = soup.select_one(".ajd_header__job-info")
        location_el = (ajd.select_one(".ajd_header__location") if ajd else None) or soup.select_one(
            ".ajd_header__location")
        job_id_el = (ajd.select_one(".job-id") if ajd else None) or soup.select_one("span.job-id")
        info["job_id"] = _job_id_value(job_id_el)
        category_meta = soup.select_one('meta[name="gtm_tbcn_jobcategory"]')
        info["category"] = _norm(category_meta.get("content")) if category_meta else None
    info["location_text"] = _visible_location_text(location_el)
    info["location_popup"] = _popup_locations(location_el)
    info["location_source"] = "visible job-info"
    if location_el is not None:
        info["location_data_attr"] = location_el.get("data-drew")
    if not info.get("category"):
        category_meta = soup.select_one('meta[name="gtm_tbcn_jobcategory"]')
        if category_meta:
            info["category"] = _norm(category_meta.get("content"))
    return info, template


def _locations(info: dict, ld: dict) -> list[dict]:
    if info.get("location_popup"):
        values = list(info["location_popup"])
        source = "visible campaign location modal"
    elif info.get("location_text"):
        values = [part.strip() for part in info["location_text"].split(";") if part.strip()]
        source = "visible job-info"
    elif info.get("location_data_attr"):
        values = [part.strip() for part in info["location_data_attr"].split(";") if part.strip()]
        source = "job-info data attribute"
    else:
        values = []
        for place in ld.get("jobLocation") or []:
            address = place.get("address") or {}
            parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            value = ", ".join(part for part in parts if part)
            if value:
                values.append(value)
        source = "json-ld jobLocation"
    return [{"location": value, "source": source} for value in values]


def _workplace_type(locations: list[dict]) -> str | None:
    values = [loc["location"] for loc in locations]
    if values and all(re.match(r"remote\b", value, re.I) for value in values):
        return "Remote"
    return None


def parse_salary(description_text: str) -> dict | None:
    """Retain the published pay-range prose verbatim; never convert it."""
    index = description_text.lower().find(PAY_HEADING.lower())
    if index < 0:
        return None
    tail = description_text[index:].strip()
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    amount_lines = []
    for line in lines[1:]:
        if HEADING_RE.match(line):
            break
        amount_lines.append(line)
    return {
        "source": "jobs.intuit.com role description",
        "heading": lines[0] if lines else PAY_HEADING,
        "amount_lines": amount_lines,
        "raw": "\n".join([lines[0], *amount_lines]) if lines else PAY_HEADING,
    }


def _sections(container) -> list[dict]:
    sections: list[dict] = []
    current = None
    for node in container.find_all(recursive=False):
        heading = None
        if node.name in ("h2", "h3", "h4"):
            heading = node.get_text(" ", strip=True)
        elif node.name == "p":
            text = node.get_text(" ", strip=True)
            classes = [str(value).lower() for value in node.get("class") or []]
            if any(value in ("responsibilities", "qualifications") for value in classes):
                heading = text
            elif len(text) <= 80 and HEADING_RE.match(text):
                heading = text
        if heading:
            current = {"heading": heading, "items": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if node.name in ("ul", "ol"):
            for li in node.find_all("li", recursive=False):
                value = _norm(li.get_text(" ", strip=True))
                if value:
                    current["items"].append(value)
        else:
            value = _norm(node.get_text(" ", strip=True))
            if value:
                current["items"].append(value)
    return sections


def _pick(sections: list[dict], pattern: str) -> list[str] | None:
    values = [item for section in sections
              if re.search(pattern, section["heading"], re.I)
              for item in section["items"]]
    return values or None


def _jsonld(soup: BeautifulSoup) -> dict | None:
    for script in soup.select('script[type="application/ld+json"]'):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") in ("JobPosting", ["JobPosting"]):
                return candidate
    return None


def _apply_url(soup: BeautifulSoup, meta: dict, *, detail_url: str, job_id: str) -> tuple[str | None, list[str]]:
    warnings = []
    apply_url = None
    apply_anchor = soup.select_one("a.job-apply[data-apply-url], a.job-apply[href]")
    if apply_anchor is not None:
        apply_url = apply_anchor.get("data-apply-url") or apply_anchor.get("href")
    if not apply_url:
        apply_url = meta.get("search-job-apply-url") or None
    if not apply_url:
        return None, ["missing_apply_url"]
    parsed = urlsplit(apply_url)
    if parsed.scheme != "https" or parsed.hostname not in APPLY_HOSTS:
        raise ValueError(f"Intuit detail {detail_url} apply link left the observed issuers: {apply_url}")
    if parsed.hostname == "www.intuit.com" and not parsed.path.startswith(("/expert-careers/", "/payrolltaxexpert", "/SeniorServiceandSupport")):
        raise ValueError(f"Intuit detail {detail_url} campaign apply link has an unexpected path: {apply_url}")
    if job_id and job_id not in apply_url:
        warnings.append("apply_url_does_not_carry_visible_job_id")
    return apply_url, warnings


def parse_detail(html: str, *, row: dict, detail_url: str) -> dict:
    """Parse one exact published Intuit job detail page."""
    parsed_url = urlsplit(detail_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != CAREERS_HOST:
        raise ValueError(f"Intuit detail left its official host: {detail_url}")
    if not DETAIL_ROUTE.match(parsed_url.path):
        raise ValueError(f"Intuit detail route is not the published shape: {detail_url}")
    soup = BeautifulSoup(html, "lxml")
    ld = _jsonld(soup)
    if ld is None:
        raise ValueError(f"Intuit detail {detail_url} has no JobPosting JSON-LD")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title:
        raise ValueError(f"Intuit detail {detail_url} has no visible job title")
    if _norm(ld.get("title")) != _norm(title):
        raise ValueError(f"Intuit detail {detail_url} JSON-LD title differs: {ld.get('title')!r} vs {title!r}")
    ld_url = str(ld.get("url") or "")
    if urlsplit(ld_url).path.rstrip("/") != parsed_url.path.rstrip("/"):
        raise ValueError(f"Intuit detail {detail_url} JSON-LD url differs: {ld_url!r}")
    info, template = _parse_info(soup)
    visible_job_id = info.get("job_id")
    card_job_id = row.get("card_job_id")
    url_job_id = row.get("url_job_id")
    job_id = visible_job_id or card_job_id or url_job_id
    if not job_id:
        raise ValueError(f"Intuit detail {detail_url} has no visible or listing job ID")
    identifier = ld.get("identifier") or {}
    ld_job_id = str(identifier.get("value")) if isinstance(identifier, dict) else None
    acceptable_ids = {value for value in (card_job_id, url_job_id, visible_job_id) if value}
    if ld_job_id not in acceptable_ids:
        raise ValueError(
            f"Intuit detail {detail_url} identifier {ld_job_id!r} not in observed IDs {sorted(acceptable_ids)}")
    id_variant = "campaign_label" if card_job_id and visible_job_id and card_job_id != visible_job_id else "ats_job_id"
    container = soup.select_one("#job-team")
    description_container = "#job-team"
    if container is None:
        container = soup.select_one(".ajd_job-details .ats-description") or soup.select_one(".ats-description")
        description_container = ".ats-description"
    if container is None:
        raise ValueError(f"Intuit detail {detail_url} has no visible description container")
    description_html = container.decode_contents()
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"Intuit detail {detail_url} description is implausibly short")
    visible = _norm(description_text)
    if visible.startswith("Job Overview "):
        visible = visible[len("Job Overview "):]
    ld_text = text_html(ld.get("description") or "")
    ld_norm = _norm(ld_text)
    jsonld_ratio = _similarity(_canonical(visible), _canonical(ld_norm))
    if jsonld_ratio < 0.98:
        raise ValueError(
            f"Intuit detail {detail_url} visible description disagrees with JSON-LD ({jsonld_ratio:.4f})")
    locations = _locations(info, ld)
    meta = {}
    for tag in soup.select("meta[name]"):
        name = tag.get("name")
        if name and (name.startswith("search-job-") or name in {
                "job-ats-req-id", "job-category-ids", "job-location-ids", "gtm_tbcn_jobcategory",
                "gtm_jobid", "job-tbcn-job-title"}):
            meta[name] = (tag.get("content") or "").strip()
    apply_url, apply_warnings = _apply_url(
        soup, meta, detail_url=detail_url, job_id=job_id)
    date_raw = ld.get("datePosted")
    date_posted = date_raw if isinstance(date_raw, str) and DATE_RE.match(date_raw) else None
    if date_posted:
        try:
            date_posted = datetime_strptime(date_posted)
        except ValueError:
            date_posted = None
    sections = _sections(container)
    responsibilities = _pick(sections, r"responsibilit|what you(?:'|’)ll do")
    qualifications = _pick(
        sections, r"qualification|requirements|what you(?:'|’)ll need|what you bring|minimum")
    salary = parse_salary(description_text)
    base_salary = ld.get("baseSalary")
    if _has_base_salary(base_salary):
        if salary is None:
            salary = {"source": "json-ld baseSalary", "baseSalary": base_salary}
        else:
            salary["jsonld_baseSalary"] = base_salary
    overview = soup.select_one("#job-description-wrapper")
    overview_html = overview.decode_contents() if overview is not None else None
    overview_text = text_html(overview_html) if overview_html else None
    if overview_text is not None and not overview_text.strip():
        overview_text = None
    jsonld_meta = {key: value for key, value in ld.items() if key != "description"}
    return {
        "title": title,
        "template": template,
        "description_container": description_container,
        "description_html": description_html,
        "description_text": description_text,
        "visible_normalized": visible,
        "jsonld_meta": jsonld_meta,
        "jsonld_description_chars": len(ld.get("description") or ""),
        "jsonld_description_text_chars": len(ld_norm),
        "jsonld_description_sha256": hashlib.sha256(_canonical(ld_norm).encode()).hexdigest(),
        "visible_description_sha256": hashlib.sha256(_canonical(visible).encode()).hexdigest(),
        "jsonld_ratio": round(jsonld_ratio, 6),
        "jsonld_match": _canonical(visible) == _canonical(ld_norm),
        "job_id": job_id,
        "visible_job_id": visible_job_id,
        "card_job_id": card_job_id,
        "url_job_id": url_job_id,
        "jsonld_identifier": ld_job_id,
        "id_variant": id_variant,
        "date_posted": date_posted,
        "date_posted_raw": date_raw,
        "employment_type": ld.get("employmentType"),
        "employer": (ld.get("hiringOrganization") or {}).get("name") if isinstance(ld.get("hiringOrganization"), dict) else None,
        "locations": locations,
        "workplace_type": _workplace_type(locations),
        "info": info,
        "meta": meta,
        "apply_url": apply_url,
        "apply_warnings": apply_warnings,
        "salary": salary,
        "sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "company_overview_html": overview_html or None,
        "company_overview_text": overview_text,
        "job_location_ids": meta.get("job-location-ids"),
    }


def _has_base_salary(base_salary) -> bool:
    """True only when the JSON-LD baseSalary actually publishes an amount."""
    if not isinstance(base_salary, dict):
        return False
    if base_salary.get("currency"):
        return True
    value = base_salary.get("value")
    if isinstance(value, dict):
        return bool(value.get("value") or value.get("minValue") or value.get("maxValue"))
    return bool(value)


def datetime_strptime(value: str) -> str:
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


class Intuit(Adapter):
    """Follow Intuit's own paged search and exact native job pages."""

    VERIFICATION_REVISION = "intuit.talentbrew-search-showall-and-native-details.2"

    def _check_listing_url(self, url: str) -> None:
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != CAREERS_HOST:
            raise ValueError(f"Intuit listing left its official host: {url}")
        if parsed.path.rstrip("/") not in ("/search-jobs", "/search-jobs/results"):
            raise ValueError(f"Intuit listing has an unexpected path: {url}")

    async def _results(self, *, page: int, per_page: int) -> tuple[list[dict], dict, dict]:
        params = search_params(page=page, per_page=per_page)
        payload = await self.http.json(RESULTS_URL, params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), str):
            raise ValueError(f"Intuit search response is not the expected results payload: {RESULTS_URL}")
        request_url = str(self.http.client.build_request("GET", RESULTS_URL, params=params).url)
        rows, meta = parse_results_fragment(payload["results"], request_url=request_url)
        if meta["per_page"] != per_page:
            self.result.warnings.append(
                f"intuit_records_per_page_changed: requested {per_page}, got {meta['per_page']}")
        return rows, meta, payload

    async def _showall(self) -> tuple[list[dict], dict, int]:
        """Fetch the coherent show-all search snapshot, following its own pages."""
        rows, meta, _ = await self._results(page=1, per_page=1000)
        all_rows = list(rows)
        total_pages = max(1, int(meta["total_pages"]))
        for page in range(2, total_pages + 1):
            if not self.page_guard():
                raise ValueError("Intuit show-all page budget exhausted")
            more, page_meta, _ = await self._results(page=page, per_page=1000)
            if page_meta["total"] != meta["total"]:
                self.result.warnings.append("intuit_showall_total_changed_between_pages")
            all_rows.extend(more)
        return all_rows, meta, total_pages

    def _make_job(self, row: dict, detail: dict):
        raw = {
            "listing": row,
            "listing_page_url": row["request_url"],
            "detail": {
                "template": detail["template"],
                "description_container": detail["description_container"],
                "info": detail["info"],
                "meta": detail["meta"],
                "jsonld": detail["jsonld_meta"],
                "jsonld_match": detail["jsonld_match"],
                "jsonld_ratio": detail["jsonld_ratio"],
                "jsonld_description_chars": detail["jsonld_description_chars"],
                "jsonld_description_text_chars": detail["jsonld_description_text_chars"],
                "jsonld_description_sha256": detail["jsonld_description_sha256"],
                "visible_description_sha256": detail["visible_description_sha256"],
                "sections": detail["sections"],
                "company_overview_html": detail["company_overview_html"],
                "company_overview_text": detail["company_overview_text"],
                "job_location_ids": detail["job_location_ids"],
                "id_variant": detail["id_variant"],
                "visible_job_id": detail["visible_job_id"],
                "card_job_id": detail["card_job_id"],
                "url_job_id": detail["url_job_id"],
                "jsonld_identifier": detail["jsonld_identifier"],
                "apply_warnings": detail["apply_warnings"],
            },
            "source_ui": {
                "listing_title": row["title"],
                "listing_location": row["location_label"],
                "listing_category": row["category"],
                "listing_category_id": row["category_id"],
                "listing_remote_attr": row["remote_attr"],
                "card_job_id": row["card_job_id"],
                "url_job_id": row["url_job_id"],
                "detail_url": row["url"],
            },
        }
        job = self.job(
            raw,
            title=detail["title"],
            url=row["url"],
            job_id=detail["job_id"],
            requisition_id=detail["job_id"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="intuit.talentbrew.native_job_page.job-team",
            description_url=row["url"],
            locations=detail["locations"],
            department=detail["info"].get("category") or row.get("category"),
            employment_type=detail["employment_type"],
            workplace_type=detail["workplace_type"],
            date_posted=detail["date_posted"],
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            salary=detail["salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            employer=detail["employer"] or "Intuit",
        )
        job.description_text = detail["description_text"]
        job.record_identity = f"intuit:{row['url_job_id']}"
        if _norm(detail["title"]) != _norm(row["title"]):
            job.warnings.append("listing_title_differs_from_detail_title")
        job.warnings.extend(detail["apply_warnings"])
        if detail["jsonld_ratio"] < 1.0:
            job.warnings.append("jsonld_description_quote_or_heading_difference")
        if detail["id_variant"] == "campaign_label":
            job.warnings.append("campaign_label_identifier_with_visible_ats_job_id")
        return job

    async def _detail_for(self, job):
        response = await self.http.get(job.url)
        if str(response.url).rstrip("/") != job.url.rstrip("/"):
            raise ValueError(f"Intuit detail {job.job_id} redirected to {response.url}")
        parsed = parse_detail(response.text, row=job.raw["listing"], detail_url=job.url)
        fresh = self._make_job(job.raw["listing"], parsed)
        fresh.description_verified = job.description_verified
        return fresh

    async def _ssr_page(self, page: int) -> tuple[list[dict], dict]:
        url = f"{BASE}/search-jobs?p={page}"
        response = await self.http.get(url)
        parsed = urlsplit(str(response.url))
        if (parsed.scheme != "https" or parsed.hostname != CAREERS_HOST
                or parsed.path.rstrip("/") != "/search-jobs"):
            raise ValueError(f"Intuit SSR listing left its official route: {response.url}")
        rows, meta = parse_listing_page(response.text, page_url=url)
        if meta["current"] != page:
            raise ValueError(f"Intuit SSR page {page} reports current page {meta['current']}")
        return rows, meta

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        entry = urlsplit(entry_url)
        if entry.scheme != "https" or entry.hostname != CAREERS_HOST or entry.path.rstrip("/") != "/search-jobs":
            raise ValueError("Intuit dedicated flow must start from its official /search-jobs entry")

        # ---- membership: coherent show-all search snapshot ----
        listing_started = time.perf_counter()
        if not self.page_guard():
            return self.result
        rows_a, meta_a, _ = await self._showall()
        total_a = meta_a["total"]
        if total_a <= 0 or not rows_a:
            raise ValueError("Intuit search reports zero jobs; independent review required")
        rows_a_by_id = {row["url_job_id"]: row for row in rows_a}
        if len(rows_a_by_id) != len(rows_a):
            raise ValueError("Intuit show-all snapshot repeats a URL job ID")
        base_ids = [row["url_job_id"] for row in rows_a]
        showall_seconds = time.perf_counter() - listing_started

        # ---- reconcile with the site's normal 15-row search pagination ----
        paginated_started = time.perf_counter()
        pages = max(1, math.ceil(total_a / 15))
        paged_rows: list[dict] = []
        page_counts: dict[int, int] = {}
        paged_meta: list[dict] = []
        for page in range(1, pages + 1):
            if not self.page_guard():
                self.result.errors.append("intuit_page_budget_exhausted")
                break
            rows, meta, _ = await self._results(page=page, per_page=15)
            if meta["current"] != page:
                raise ValueError(f"Intuit page {page} reports current page {meta['current']}")
            if meta["total"] != total_a:
                self.result.warnings.append(
                    f"intuit_total_changed_during_pagination: page {page} reports {meta['total']}")
            if meta["total_pages"] != pages:
                self.result.warnings.append(
                    f"intuit_page_count_changed_during_pagination: page {page} reports {meta['total_pages']}")
            page_counts[page] = len(rows)
            paged_meta.append({"page": page, "rows": len(rows), "total": meta["total"],
                               "total_pages": meta["total_pages"]})
            paged_rows.extend(rows)
        paged_ids = [row["url_job_id"] for row in paged_rows]
        paged_unique = set(paged_ids)
        paginated_seconds = time.perf_counter() - paginated_started
        only_showall = [job_id for job_id in base_ids if job_id not in paged_unique]
        only_paged = [job_id for job_id in paged_unique if job_id not in rows_a_by_id]
        if len(paged_ids) != len(paged_unique):
            self.result.errors.append("intuit_paginated_listing_repeats_ids")
        if paged_unique != rows_a_by_id.keys():
            self.result.warnings.append("intuit_showall_and_paginated_ids_differ")

        # ---- reconcile the server-rendered listing pages (visible fallback) ----
        ssr_started = time.perf_counter()
        if not self.page_guard():
            self.result.errors.append("intuit_ssr_page_budget_exhausted")
            ssr_rows: list[dict] = []
            ssr_first_meta = {"total": 0, "total_pages": 0, "h1": None, "pager_label": None}
            ssr_page_counts: dict[int, int] = {}
        else:
            ssr_rows, ssr_first_meta = await self._ssr_page(1)
            ssr_page_counts = {1: len(ssr_rows)}
            ssr_total_pages = max(1, int(ssr_first_meta["total_pages"]))
            ssr_page_meta = [{"page": 1, "rows": len(ssr_rows), "total": ssr_first_meta["total"],
                              "total_pages": ssr_first_meta["total_pages"]}]
            for page in range(2, ssr_total_pages + 1):
                if not self.page_guard():
                    self.result.errors.append("intuit_ssr_page_budget_exhausted")
                    break
                more, meta = await self._ssr_page(page)
                if meta["total"] != ssr_first_meta["total"]:
                    self.result.warnings.append(
                        f"intuit_ssr_total_changed_during_walk: page {page} reports {meta['total']}")
                ssr_page_counts[page] = len(more)
                ssr_page_meta.append({"page": page, "rows": len(more), "total": meta["total"],
                                      "total_pages": meta["total_pages"]})
                ssr_rows.extend(more)
        ssr_seen: dict[str, dict] = {}
        ssr_order: list[str] = []
        ssr_duplicates: list[str] = []
        for row in ssr_rows:
            job_id = row["url_job_id"]
            if job_id in ssr_seen:
                ssr_duplicates.append(job_id)
                continue
            ssr_seen[job_id] = row
            ssr_order.append(job_id)
        ssr_only_rows = [ssr_seen[job_id] for job_id in ssr_order if job_id not in rows_a_by_id]
        ssr_only_ids = [row["url_job_id"] for row in ssr_only_rows]
        ajax_only_ids = [job_id for job_id in base_ids if job_id not in ssr_seen]
        ssr_seconds = time.perf_counter() - ssr_started

        # The search widget is the coherent primary snapshot; the visible SSR
        # fallback contributes any posting that is currently advertised there
        # but missing from the widget's index (observed 24496).
        membership_ids = list(base_ids)
        for row in ssr_only_rows:
            rows_a_by_id[row["url_job_id"]] = row
            membership_ids.append(row["url_job_id"])

        # ---- details: every exact published URL ----
        detail_started = time.perf_counter()
        pending = []
        hydrated_ids = membership_ids
        if self.options.max_jobs:
            hydrated_ids = membership_ids[: self.options.max_jobs]
            self.result.warnings.append("sample_limit")
        for job_id in hydrated_ids:
            row = rows_a_by_id[job_id]
            pending.append(self.job({"listing": row}, title=row["title"], url=row["url"], job_id=job_id))
            pending[-1].record_identity = f"intuit:{job_id}"
        hydrated = await self.hydrate(pending, self._detail_for)
        detail_seconds = time.perf_counter() - detail_started

        # ---- final listing recheck; collect any newly advertised rows ----
        final_started = time.perf_counter()
        rows_b, meta_b, _ = await self._showall()
        final_base_ids = [row["url_job_id"] for row in rows_b]
        rows_b_by_id = {row["url_job_id"]: row for row in rows_b}
        if len(rows_b_by_id) != len(rows_b):
            raise ValueError("Intuit final show-all snapshot repeats a URL job ID")
        new_ids = [job_id for job_id in final_base_ids if job_id not in rows_a_by_id]
        removed_ids = [job_id for job_id in base_ids if job_id not in rows_b_by_id]
        final_ids = list(final_base_ids) + [job_id for job_id in ssr_only_ids
                                            if job_id not in rows_b_by_id]
        extra_jobs = []
        for job_id in new_ids:
            if job_id in rows_a_by_id:
                continue
            row = rows_b_by_id[job_id]
            job = self.job({"listing": row}, title=row["title"], url=row["url"], job_id=job_id)
            job.record_identity = f"intuit:{job_id}"
            try:
                refreshed = await self._detail_for(job)
            except Exception as exc:
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(f"Detail {row['url']}: {type(exc).__name__}: {exc}")
                extra_jobs.append(job)
                continue
            extra_jobs.append(refreshed)
        final_seconds = time.perf_counter() - final_started

        by_id = {job.raw["listing"]["url_job_id"]: job for job in hydrated}
        by_id.update({job.raw["listing"]["url_job_id"]: job for job in extra_jobs})
        removed = []
        for job_id in removed_ids:
            job = by_id.pop(job_id, None)
            if job is not None:
                removed.append({"url_job_id": job_id, "job_id": job.job_id,
                                "url": job.url, "title": job.title})
        for job_id in final_ids:
            job = by_id.get(job_id)
            if job is not None:
                self.add(job)

        self.result.expected_total = len(final_ids)
        self.result.pages = pages + ssr_total_pages + 2 if "ssr_total_pages" in locals() else pages + 2
        self.result.listing_complete = (
            len(self.result.jobs) == len(final_ids)
            and not any(error.startswith("Detail ") for error in self.result.errors)
            and not any(error in ("intuit_paginated_listing_repeats_ids", "intuit_page_budget_exhausted",
                                  "intuit_ssr_page_budget_exhausted")
                        for error in self.result.errors)
        )
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "listing_url": ENTRY_URL,
            "listing_request_url": RESULTS_URL,
            "membership_authority": (
                "Intuit official jobs.intuit.com TalentBrew search widget, unfiltered, tenant 27595; "
                "coherent show-all snapshot cross-checked against every 15-row widget page and the "
                "server-rendered ?p=N fallback listing"
            ),
            "listing_request_params": search_params(page=1, per_page=1000),
            "pagination_request_params": search_params(page=2, per_page=15),
            "listing_filter_behavior": "No keywords, location, facet or category filters applied; default sort",
            "ui_pagination": "41 result pages at 15 rows per page with numbered page input; server-rendered ?p=N fallback",
            "org_id": ORG_ID,
            "total_initial_snapshot": total_a,
            "total_final_snapshot": len(final_base_ids),
            "showall_rows": len(rows_a),
            "paginated_pages": pages,
            "page_row_counts": page_counts,
            "paged_meta": paged_meta,
            "showall_and_paginated_ids_match": paged_unique == rows_a_by_id.keys() - set(ssr_only_ids),
            "only_in_showall": only_showall,
            "only_in_paginated": only_paged,
            "initial_ids_in_order": base_ids,
            "final_ids_in_order": final_ids,
            "final_widget_ids_in_order": final_base_ids,
            "new_ids_during_run": new_ids,
            "removed_ids_during_run": removed_ids,
            "removed_records": removed,
            "new_rows_fetched": [job.raw["listing"]["url_job_id"] for job in extra_jobs],
            "ssr_reconciliation": {
                "pages": ssr_total_pages if "ssr_total_pages" in locals() else 0,
                "page_row_counts": ssr_page_counts,
                "cards": len(ssr_rows),
                "unique_ids": len(ssr_seen),
                "duplicate_ids": ssr_duplicates,
                "header_h1": ssr_first_meta.get("h1"),
                "pager_label": ssr_first_meta.get("pager_label"),
                "ssr_only_ids": ssr_only_ids,
                "ajax_only_ids": ajax_only_ids,
                "union_count": len(base_ids) + len(ssr_only_ids),
                "policy": (
                    "The coherent widget snapshot is the primary membership; SSR-only IDs are live "
                    "server-rendered listing rows absent from the widget index and are collected too. "
                    "SSR page shifts can duplicate/skip rows, so its union is reconciled against the widget."
                ),
            },
            "description_source": (
                "#job-team visible HTML on each exact published /job/... page; JSON-LD JobPosting.description "
                "corroborated (quote/heading-normalised)"
            ),
            "company_overview_note": (
                "The separate #job-description-wrapper 'Company Overview' template block (389 chars, absent on "
                "some templates) is retained per-job raw but excluded from role description, matching the "
                "site's JSON-LD JobPosting.description"
            ),
            "salary_policy": "Published expected-pay prose retained verbatim; no conversion; empty JSON-LD baseSalary stays null",
            "apply_route": (
                "Per-job visible apply link retained: intuit.avature.net (standard), "
                "www.intuit.com/expert-careers (seasonal tax campaign) or talenteq.intuit.com; "
                "no ATS board enumerated"
            ),
            "id_policy": (
                "job_id/requisition_id is the visible Job ID (ATS req); url_job_id is the published URL's "
                "final segment; 42 seasonal tax cards publish a campaign label as card/JSON-LD identifier "
                "and are reconciled to the visible numeric ATS job ID"
            ),
            "listing_phase_seconds": round(showall_seconds + paginated_seconds + ssr_seconds + final_seconds, 6),
            "showall_initial_seconds": round(showall_seconds, 6),
            "paginated_reconciliation_seconds": round(paginated_seconds, 6),
            "ssr_reconciliation_seconds": round(ssr_seconds, 6),
            "final_recheck_seconds": round(final_seconds, 6),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["listing_phase_seconds"] = round(
            showall_seconds + paginated_seconds + ssr_seconds + final_seconds, 6)
        self.result.timings["showall_initial_seconds"] = round(showall_seconds, 6)
        self.result.timings["paginated_reconciliation_seconds"] = round(paginated_seconds, 6)
        self.result.timings["ssr_reconciliation_seconds"] = round(ssr_seconds, 6)
        self.result.timings["final_recheck_seconds"] = round(final_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        return self.result
