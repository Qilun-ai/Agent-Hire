"""Société Générale's own careers search (Quantum/CES) and its job detail pages."""
from __future__ import annotations

import gzip
import json
import re
import time
from hashlib import sha256
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

OFFICIAL_ENTRY = "https://careers.societegenerale.com/en/search"
ORIGIN = "https://careers.societegenerale.com"
TOKEN_URL = ORIGIN + "/sg-careers-offers/get-token"
PROXY_URL = ORIGIN + "/search-proxy.php"
DEFAULT_PROFILE_URL = (
    "https://api.socgen.com/business-support/it-for-it-support/"
    "cognitive-service-knowledge/api/v1/search-profile"
)
PROFILE_NAME = "ces_profile_sgcareers"
JOB_FILTER = {"type": "simple", "name": "sourcestr6", "op": "eq", "value": "job"}
PAGE_SIZE = 1000
MAX_PAGES = 40
BOILERPLATE_SECTION_IDS = ("job-detail-diversite", "job-detail-transparence")
ROLE_SECTION_IDS = ("job-detail-description", "job-detail-profile",
                    "job-detail-evolution", "job-detail-group")
DETAIL_PATH = re.compile(
    r"^/(?:en/)?(?:job-offers|offres-d-emploi)/[a-z0-9][a-z0-9-]*-(?P<ref>[A-Z0-9]{6,10})-(?P<lang>en|fr)$",
    re.I,
)
TALEO_APPLY = re.compile(
    r"^https://socgen\.taleo\.net/careersection/sgcareers/jobapply\.ftl\?job=(?P<ref>[A-Z0-9]+)(?:&|$)"
)
CHALLENGE_STATUS = (401, 403, 429)
# The search engine assigns a new result id / result URLs on every query; they
# identify the query response, not the posting, and are excluded from the
# initial-vs-final field comparison.
VOLATILE_LISTING_FIELDS = (
    "resultid", "docrank",
    "urldocresult", "urldocresulthorz", "urldocresultleft",
    "urldocresultontext", "urldocresultvert",
)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def normalized_text(value: str | None) -> str:
    """Whitespace-normalised comparison text (HTML inline joins add stray spaces)."""
    text = re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()
    text = re.sub(r"\s+([,;:!?%)»])", r"\1", text)
    return re.sub(r"([(«])\s+", r"\1", text)


def detail_url_parts(url: str) -> tuple[str, str]:
    """Return (requisition, language) for an exact Société Générale job URL."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "careers.societegenerale.com":
        raise ValueError(f"Not a Société Générale detail URL: {url}")
    match = DETAIL_PATH.fullmatch(parts.path)
    if match is None:
        raise ValueError(f"Unexpected Société Générale detail path: {parts.path}")
    return match.group("ref").upper(), match.group("lang").lower()


def _settings_from_soup(soup: BeautifulSoup) -> dict:
    node = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
    if node is None or not node.get_text(strip=True):
        return {}
    try:
        value = json.loads(node.get_text())
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _drupal_settings(html: str) -> dict:
    return _settings_from_soup(BeautifulSoup(html, "lxml"))


def parse_search_settings(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the Drupal/Quantum settings that drive the official listing widget."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "careers.societegenerale.com"
            or parts.path.rstrip("/") != "/en/search"):
        raise ValueError("Société Générale search page redirected away from the inspected URL")
    data = _drupal_settings(html)
    quantum = data.get("quantum")
    if not isinstance(quantum, dict):
        raise ValueError("Société Générale search page no longer exposes Quantum widget settings")
    profile_url = quantum.get("base_url")
    if not isinstance(profile_url, str) or not profile_url.startswith("https://api.socgen.com/"):
        raise ValueError("Société Générale Quantum API base URL changed")
    csrf = data.get("csrfToken")
    if not isinstance(csrf, str) or not re.fullmatch(r"[0-9a-f]{64}", csrf):
        raise ValueError("Société Générale search page CSRF token changed")
    filters = quantum.get("quantum_filters") or {}
    if not isinstance(filters.get("refContrat"), dict) or not isinstance(filters.get("refLocation"), dict):
        raise ValueError("Société Générale listing filter definitions changed")
    if not isinstance(filters["refLocation"].get("id"), dict):
        raise ValueError("Société Générale location id map changed")
    all_offers = quantum.get("all_offers")
    if not isinstance(all_offers, dict) or not all_offers:
        raise ValueError("Société Générale offer/location map changed")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Societe Generale" not in title or "job" not in title.lower():
        raise ValueError("Société Générale search page identity changed")
    return {
        "url": response_url,
        "title": title,
        "csrf_token": csrf,
        "profile_url": profile_url.rstrip("/") + "/search-profile",
        "profile_name": PROFILE_NAME,
        "filters": filters,
        "all_offers": all_offers,
        "last_offers": quantum.get("last_offers") or [],
    }


def parse_search_page(payload: dict, *, skip_count: int, skip_from: int) -> dict:
    """Validate one exact Quantum search response and return its listing rows."""
    if not isinstance(payload, dict):
        raise ValueError("Société Générale listing response is not an object")
    if payload.get("Profile") != PROFILE_NAME:
        raise ValueError(f"Société Générale listing profile changed: {payload.get('Profile')!r}")
    result = payload.get("Result")
    keys = ("TotalCount", "DocumentCount", "PageCount", "DocumentsPerPage",
            "DocumentsThisPage", "CurrentPage")
    totals = {key: payload.get(key) for key in keys}
    if not isinstance(result, dict) or any(not isinstance(value, int) for value in totals.values()):
        raise ValueError("Société Générale listing pagination fields changed")
    docs = result.get("Docs")
    if not isinstance(docs, list):
        raise ValueError("Société Générale listing response is missing Result.Docs")
    issues, rows, seen = [], [], set()
    for doc in docs:
        if not isinstance(doc, dict):
            issues.append("listing_row_not_object")
            continue
        docid = doc.get("docid")
        identity = doc.get("sourcestr12")
        ref = doc.get("sourcestr4")
        try:
            url_ref, url_lang = detail_url_parts(doc.get("resulturl") or "")
        except ValueError:
            issues.append(f"listing_row_bad_detail_url:{identity or docid}")
            continue
        if not docid or not identity or not ref:
            issues.append(f"listing_row_missing_identity:{docid}")
            continue
        if doc.get("sourcestr6") != "job":
            issues.append(f"listing_row_not_a_job:{identity}")
            continue
        if doc.get("languages") not in ("en", "fr"):
            issues.append(f"listing_row_unknown_language:{identity}")
            continue
        if url_ref != str(ref).upper() or url_lang != doc["languages"]:
            issues.append(f"listing_row_url_mismatch:{identity}")
            continue
        if identity in seen:
            issues.append(f"listing_row_duplicate_docid:{identity}")
            continue
        if not normalized_text(doc.get("title")):
            issues.append(f"listing_row_missing_title:{identity}")
            continue
        seen.add(identity)
        rows.append(doc)
    if totals["TotalCount"] and not rows:
        issues.append("listing_response_has_no_rows")
    if totals["DocumentsThisPage"] != len(docs):
        issues.append("listing_documents_this_page_mismatch")
    expected_rows = min(skip_count, max(0, totals["TotalCount"] - skip_from))
    if len(docs) != expected_rows:
        issues.append("listing_page_row_count_mismatch")
    expected_page = skip_from // skip_count + 1
    if totals["CurrentPage"] != expected_page:
        issues.append("listing_current_page_mismatch")
    return {
        "total_count": totals["TotalCount"],
        "document_count": totals["DocumentCount"],
        "page_count": totals["PageCount"],
        "documents_per_page": totals["DocumentsPerPage"],
        "current_page": totals["CurrentPage"],
        "requested_skip_count": skip_count,
        "requested_skip_from": skip_from,
        "rows": rows,
        "issues": list(dict.fromkeys(issues)),
    }


def listing_membership(docs: list[dict]) -> dict:
    """Reconcile published listing rows with distinct requisitions."""
    by_ref: dict[str, list[dict]] = {}
    for doc in docs:
        by_ref.setdefault(str(doc["sourcestr4"]), []).append(doc)
    bilingual = {ref: rows for ref, rows in by_ref.items() if len({row["languages"] for row in rows}) > 1}
    languages = {"en": 0, "fr": 0}
    for doc in docs:
        languages[doc["languages"]] = languages.get(doc["languages"], 0) + 1
    return {
        "listing_rows": len(docs),
        "distinct_requisitions": len(by_ref),
        "bilingual_requisitions": len(bilingual),
        "bilingual_requisition_ids": sorted(bilingual),
        "language_rows": languages,
    }


def location_text(ref: str, language: str, all_offers: dict, location_map: dict) -> dict:
    """Reproduce the listing widget's city, region, country label."""
    entry = all_offers.get(ref) or {}
    values = []
    for kind in ("city", "region", "country"):
        identifier = entry.get(kind)
        if not identifier:
            continue
        label = (location_map.get(identifier) or {}).get(language) or identifier
        if not values or values[-1] != label:
            values.append(label)
    return {
        "city": (location_map.get(entry.get("city")) or {}).get(language) if entry.get("city") else None,
        "region": (location_map.get(entry.get("region")) or {}).get(language) if entry.get("region") else None,
        "country": (location_map.get(entry.get("country")) or {}).get(language) if entry.get("country") else None,
        "location_ids": {kind: entry.get(kind) for kind in ("city", "region", "country") if entry.get(kind)},
        "text": ", ".join(values),
    }


def _visible_date(match):
    if not match:
        return None
    value = match.group(1)
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        day, month, year = value.split("/")
        return f"{year}-{month}-{day}"
    return value.replace("/", "-")


def parse_detail_page(html: str, url: str = "") -> dict:
    """Read the public fields and every role section of one exact job page."""
    soup = BeautifulSoup(html, "lxml")
    crawl_node = soup.select_one("#crawl_data")
    if crawl_node is None:
        raise ValueError("Société Générale detail page is missing #crawl_data")
    crawl = {span.get("id"): span.get("data-value") for span in crawl_node.select("span[id]")}
    ref = (crawl.get("offer_ref") or "").strip().upper()
    if not ref:
        raise ValueError("Société Générale detail page is missing its offer reference")
    if url:
        url_ref, _ = detail_url_parts(url)
        if url_ref != ref:
            raise ValueError(f"Detail page reference {ref} does not match its URL")
    title_node = soup.select_one("#offerTitle")
    title = normalized_text(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        title = normalized_text(crawl.get("page_title"))
    if not title:
        raise ValueError("Société Générale detail page is missing its title")

    sections, issues = [], []
    for node in soup.select("section[id]"):
        section_id = node.get("id", "")
        if section_id not in ROLE_SECTION_IDS:
            continue
        heading = node.find("h2")
        body = heading.find_next_sibling("div") if heading is not None else None
        body_html = "".join(str(child) for child in body.contents) if body is not None else ""
        body_text = text_html(body_html)
        heading_text = normalized_text(heading.get_text(" ", strip=True)) if heading else ""
        if not body_text and not heading_text:
            continue
        sections.append({
            "id": section_id,
            "heading": heading_text,
            "html": body_html,
            "text": body_text,
            "chars": len(body_text),
        })
        if section_id == "job-detail-description" and len(body_text) < 100:
            issues.append(f"primary_section_short:{len(body_text)}")
    if not sections:
        raise ValueError("Société Générale detail page has no job-detail role sections")

    boilerplate = []
    for node in soup.select("#job-detail-diversite, #job-detail-transparence"):
        text = text_html("".join(str(child) for child in node.contents))
        if text:
            boilerplate.append({"id": node.get("id"), "chars": len(text),
                                "sha256": sha256(text.encode()).hexdigest()})

    description_html = "\n".join(
        (f"<h2>{section['heading']}</h2>" if section["heading"] else "") + section["html"]
        for section in sections
    )
    description_text = text_html(description_html)
    if not description_text.strip():
        raise ValueError("Société Générale detail page role sections are empty")

    jsonld = {}
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                jsonld = item
                break
        if jsonld:
            break

    settings = _settings_from_soup(soup)
    page_tags = ((settings.get("sg_careers_tags") or {}).get("dataLayer") or {})
    visible_text = normalized_text(soup.get_text(" ", strip=True))
    publication_visible = re.search(
        r"(?:Publication date|Date de publication)\s+(\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})", visible_text)
    start_visible = re.search(
        r"(?:Start date|Date de début)\s+(\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})", visible_text)
    reference_visible = re.search(
        r"(?:Reference|Référence)\s+([A-Z0-9]{6,10})", visible_text)

    publication_raw = crawl.get("publication_date")
    date_posted = None
    if isinstance(publication_raw, str) and re.match(r"^\d{4}-\d{2}-\d{2}", publication_raw):
        date_posted = publication_raw[:10]
    elif publication_visible:
        date_posted = publication_visible.group(1).replace("/", "-")

    salary = None
    salary_min, salary_max = (crawl.get("salary_min") or "").strip(), (crawl.get("salary_max") or "").strip()
    if salary_min or salary_max:
        salary = {"min": salary_min or None, "max": salary_max or None,
                  "currency": None, "period": None, "source": "crawl_data"}

    apply_url = crawl.get("taleo_url") or ""
    apply_ref = None
    match = TALEO_APPLY.match(apply_url)
    if match:
        apply_ref = match.group("ref")
    mission_preview = text_html(crawl.get("description") or "")
    primary = next((section for section in sections if section["id"] == "job-detail-description"), None)
    mission_matches = bool(
        primary and normalized_text(mission_preview)
        and normalized_text(mission_preview) == normalized_text(primary["text"])
    )
    return {
        "url": url,
        "ref": ref,
        "title": title,
        "crawl": crawl,
        "page_tags": page_tags,
        "sections": sections,
        "boilerplate_blocks": boilerplate,
        "description_html": description_html,
        "description_text": description_text,
        "description_chars": len(description_text),
        "description_sha256": sha256(description_text.encode()).hexdigest(),
        "jsonld": jsonld,
        "apply_url": apply_url or None,
        "apply_ref": apply_ref,
        "date_posted": date_posted,
        "date_posted_raw": {
            "detail_crawl_publication_date": publication_raw,
            "detail_jsonld_datePosted": jsonld.get("datePosted") if jsonld else None,
            "detail_visible_publication_date": _visible_date(publication_visible),
        },
        "start_date_raw": crawl.get("startup_date"),
        "start_date_visible": _visible_date(start_visible),
        "jsonld_valid_through_raw": jsonld.get("validThrough") if jsonld else None,
        "reference_visible": reference_visible.group(1) if reference_visible else None,
        "visible_location": page_tags.get("job_location"),
        "salary": salary,
        "hybrid": crawl.get("hybrid_working") == "1",
        "entity": normalized_text(crawl.get("entity")),
        "country_id": crawl.get("country_id"),
        "region_id": crawl.get("region_id"),
        "job_category_id": crawl.get("job_category_id"),
        "contract_type_id": crawl.get("contract_type_id"),
        "mission_preview_matches_primary_section": mission_matches,
        "issues": issues,
    }


def parse_sitemap_refs(xml: str) -> dict:
    """Count the company's own sitemap job URLs for cross-checking the index."""
    urls = re.findall(r"<loc>([^<]+)</loc>", xml or "")
    refs, bad = set(), []
    count = 0
    for url in urls:
        if "/job-offers/" not in url and "/offres-d-emploi/" not in url:
            continue
        try:
            ref, _ = detail_url_parts(url)
        except ValueError:
            bad.append(url)
            continue
        count += 1
        refs.add(ref)
    return {"job_urls": count, "distinct_requisitions": len(refs),
            "requisitions": sorted(refs), "unparsed_job_urls": bad}


class SocieteGenerale(Adapter):
    """Collect the company-scoped Quantum listing and every exact job detail page."""

    VERIFICATION_REVISION = "societe-generale.quantum-ces-search.1"

    def __init__(self, http, company, source, options, *, initial_snapshot=None,
                 detail_cache=None):
        super().__init__(http, company, source, options)
        self.initial_snapshot = initial_snapshot
        self.detail_cache = detail_cache or {}
        self.settings = None
        self._token = None
        self._challenge_failures = 0
        self._challenge_stopped = False

    # ---- source requests -------------------------------------------------
    async def _entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        settings = parse_search_settings(response.text, str(response.url))
        settings.update(status=response.status_code, seconds=round(seconds, 6),
                        response_sha256=sha256(response.content).hexdigest(),
                        response_bytes=len(response.content))
        self.settings = settings
        self.result.timings["entry_page_seconds"] = round(
            self.result.timings.get("entry_page_seconds", 0.0) + seconds, 6)
        self.result.metadata.setdefault("entry_requests", []).append({
            "url": str(response.url), "seconds": round(seconds, 6),
            "status": response.status_code,
            "response_sha256": sha256(response.content).hexdigest(),
            "purpose": "initial" if len(self.result.metadata["entry_requests"]) == 0 else "session_refresh",
        })
        return settings

    async def _refresh_session(self):
        """Renew the Drupal session/cookies (and CSRF) used by /search-proxy.php."""
        await self._entry()
        self._token = None

    async def _fetch_token(self, *, refresh=False):
        if self._token and not refresh:
            return self._token
        started = time.perf_counter()
        try:
            response = await self.http.request(
                "GET", TOKEN_URL,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "X-CSRF-Token": self.settings["csrf_token"],
                         "Accept": "application/json, text/javascript, */*; q=0.01",
                         "Referer": OFFICIAL_ENTRY},
            )
        except FetchError as exc:
            if "HTTP 403" not in str(exc) and "HTTP 404" not in str(exc):
                raise
            await self._refresh_session()
            response = await self.http.request(
                "GET", TOKEN_URL,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "X-CSRF-Token": self.settings["csrf_token"],
                         "Accept": "application/json, text/javascript, */*; q=0.01",
                         "Referer": OFFICIAL_ENTRY},
            )
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["token_seconds"] = round(
            self.result.timings.get("token_seconds", 0.0) + seconds, 6)
        try:
            value = response.json()
        except ValueError as exc:
            raise ValueError("Société Générale token response is not JSON") from exc
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or token.count(".") != 2:
            raise ValueError("Société Générale token response changed")
        self._token = token
        self.result.metadata.setdefault("token_requests", []).append({
            "url": str(response.request.url), "seconds": round(seconds, 6),
            "status": response.status_code, "response_sha256": sha256(response.content).hexdigest(),
            "token_sha256_prefix": sha256(token.encode()).hexdigest()[:16],
        })
        return token

    def _search_body(self, skip_count: int, skip_from: int) -> dict:
        return {
            "profile": PROFILE_NAME,
            "query": {
                "advanced": [dict(JOB_FILTER)],
                "skipCount": skip_count,
                "skipFrom": skip_from,
                "sort": "sourcedatetime1.desc",
            },
            "lang": "en",
            "responseType": "SearchResult",
        }

    async def _search(self, skip_count: int, skip_from: int) -> dict:
        body = self._search_body(skip_count, skip_from)
        last_error = None
        for attempt in range(2):
            if attempt:
                await self._refresh_session()
            token = await self._fetch_token(refresh=attempt > 0)
            started = time.perf_counter()
            try:
                response = await self.http.request(
                    "POST", PROXY_URL, json=body,
                    headers={"Content-Type": "application/json",
                             "Authorization-API": "Bearer " + token,
                             "X-Proxy-URL": self.settings["profile_url"],
                             "X-Requested-With": "XMLHttpRequest",
                             "Origin": ORIGIN, "Referer": OFFICIAL_ENTRY},
                )
            except FetchError as exc:
                last_error = exc
                if attempt == 0 and any(code in str(exc) for code in
                                        ("HTTP 401", "HTTP 403", "HTTP 404")):
                    # The proxy returns an empty 404 once the Drupal/Incapsula
                    # session cookie ages out mid-run; renew and retry once.
                    self.result.metadata["proxy_session_refreshes"] = (
                        self.result.metadata.get("proxy_session_refreshes", 0) + 1)
                    continue
                raise
            seconds = max(0.0, time.perf_counter() - started)
            page = parse_search_page(response.json(), skip_count=skip_count, skip_from=skip_from)
            page.update(request_seconds=round(seconds, 6), request_body=body,
                        status=response.status_code,
                        response_sha256=sha256(response.content).hexdigest(),
                        response_bytes=len(response.content))
            self.result.timings["listing_request_seconds_sum"] = round(
                self.result.timings.get("listing_request_seconds_sum", 0.0) + seconds, 6)
            return page
        raise last_error or RuntimeError("Société Générale listing request failed")

    async def _listing_snapshot(self, *, page_size: int = PAGE_SIZE) -> dict:
        started = time.perf_counter()
        pages, docs, seen, issues = [], [], set(), []
        expected = None
        skip = 0
        while True:
            if not self.page_guard():
                issues.append("listing_page_budget_exhausted")
                break
            page = await self._search(page_size, skip)
            if expected is None:
                expected = page["total_count"]
            elif (page["total_count"], page["page_count"]) != (expected, pages[0]["page_count"]):
                issues.append("listing_pagination_metadata_changed")
            issues.extend(page["issues"])
            for doc in page["rows"]:
                identity = doc["sourcestr12"]
                if identity in seen:
                    issues.append(f"listing_row_duplicate_across_pages:{identity}")
                    continue
                seen.add(identity)
                docs.append(doc)
            pages.append({key: page[key] for key in (
                "total_count", "document_count", "page_count", "documents_per_page",
                "current_page", "requested_skip_count", "requested_skip_from",
                "request_seconds", "status", "response_sha256", "response_bytes")} | {
                "docids": [doc["sourcestr12"] for doc in page["rows"]],
            })
            skip += page_size
            if expected is None or skip >= expected:
                break
            if len(pages) >= MAX_PAGES:
                issues.append("listing_page_budget_exhausted_before_total")
                break
        if expected is not None and len(docs) != expected:
            issues.append("listing_total_does_not_match_retained_rows")
        widget = None
        if expected:
            widget_page = await self._search(10, 0)
            widget = {"docids": [doc["sourcestr12"] for doc in widget_page["rows"]],
                      "request_seconds": widget_page["request_seconds"],
                      "response_sha256": widget_page["response_sha256"],
                      "page_count": widget_page["page_count"],
                      "total_count": widget_page["total_count"]}
            if widget["docids"] != [doc["sourcestr12"] for doc in docs[:10]]:
                issues.append("widget_first_page_mismatch")
        return {
            "docs": docs,
            "total_count": expected,
            "page_count": pages[0]["page_count"] if pages else None,
            "pages": pages,
            "widget_first_page": widget,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": round(max(0.0, time.perf_counter() - started), 6),
            "membership": listing_membership(docs),
        }

    async def _sitemap(self):
        started = time.perf_counter()
        response = await self.http.get(ORIGIN + "/sitemap.xml")
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_sitemap_refs(response.text)
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    # ---- details ---------------------------------------------------------
    def _cached_detail(self, url: str):
        entry = self.detail_cache.get(url)
        if not entry:
            return None
        try:
            raw = gzip.decompress(entry["path"].read_bytes())
        except (OSError, EOFError):
            return None
        if sha256(raw).hexdigest() != entry.get("sha256"):
            return None
        return raw.decode("utf-8", "replace")

    async def _detail(self, job):
        url = job.url
        detail = job.raw.setdefault("detail", {})
        if self._challenge_stopped:
            job.warnings.append("detail_fetch_skipped_after_source_challenge")
            self.result.errors.append(f"detail_skipped_after_challenge:{job.job_id}:{url}")
            return job
        cached_html = self._cached_detail(url)
        if cached_html is not None:
            started = time.perf_counter()
            parsed = parse_detail_page(cached_html, url)
            parsed.update(status=200, seconds=0.0,
                          response_sha256=sha256(cached_html.encode()).hexdigest(),
                          response_bytes=len(cached_html), reused_cached_response=True)
            self._apply_detail(job, parsed)
            detail["parse_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            reuse = self.result.metadata.setdefault("detail_response_reuse", {"count": 0, "urls": []})
            reuse["count"] += 1
            reuse["urls"].append(url)
            return job
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            message = str(exc)
            status_match = re.search(r"HTTP (\d{3})", message)
            status = int(status_match.group(1)) if status_match else None
            if status in CHALLENGE_STATUS:
                self._challenge_failures += 1
                if self._challenge_failures >= 3:
                    self._challenge_stopped = True
                    self.result.errors.append("source_challenge_stop")
                job.warnings.append(f"detail_source_challenge:{status}")
                self.result.errors.append(f"detail_source_challenge:{job.job_id}:{status}:{url}")
                return job
            if status in (404, 410):
                job.description_html = ""
                job.description_text = ""
                job.description_method = f"societe-generale.detail.http-{status}"
                job.description_source_url = url
                job.description_unavailable = True
                job.warnings.append(f"detail_gone_{status}")
                self.result.errors.append(f"detail_unavailable:{status}:{job.job_id}:{url}")
                detail.update({"request_url": url, "status": status, "reason": f"HTTP {status}",
                               "seconds": round(max(0.0, time.perf_counter() - started), 6)})
                return job
            raise
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail_page(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content), reused_cached_response=False)
        self._apply_detail(job, parsed)
        detail["parse_seconds"] = round(max(0.0, time.perf_counter() - started - seconds), 6)
        return job

    def _apply_detail(self, job, parsed):
        doc = job.raw["listing_doc"]
        url = str(job.url)
        job.title = parsed["title"]
        job.apply_url = parsed["apply_url"] or job.apply_url
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "societe-generale.detail.job-detail-sections"
        job.description_source_url = url
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = dict(parsed["date_posted_raw"],
                                   listing_sourcedatetime1=doc.get("sourcedatetime1"))
        job.salary = parsed["salary"]
        job.employer = parsed["entity"] or "Societe Generale"
        job.workplace_type = "Hybrid" if parsed["hybrid"] else None
        job.language = doc.get("languages")
        by_id = {section["id"]: section for section in parsed["sections"]}
        primary = by_id.get("job-detail-description")
        profile = by_id.get("job-detail-profile")
        # Some published pages keep the template's "XX" placeholders and only
        # render the corporate "Business insight" block. That is not a role
        # description; record it as unavailable instead of certifying it.
        role_content_chars = sum(
            by_id.get(section_id, {}).get("chars", 0)
            for section_id in ("job-detail-description", "job-detail-profile", "job-detail-evolution")
        )
        job.responsibilities = ({"heading": primary["heading"], "text": primary["text"],
                                 "html": primary["html"]} if primary else None)
        job.qualifications = ({"heading": profile["heading"], "text": profile["text"],
                               "html": profile["html"]} if profile else None)
        detail = job.raw["detail"]
        detail.update({
            "role_content_chars": role_content_chars,
            "request_url": url,
            "final_url": url,
            "status": parsed["status"],
            "seconds": parsed["seconds"],
            "response_sha256": parsed["response_sha256"],
            "response_bytes": parsed["response_bytes"],
            "reused_cached_response": parsed["reused_cached_response"],
            "crawl": parsed["crawl"],
            "page_tags": parsed["page_tags"],
            "sections": [{key: section[key] for key in ("id", "heading", "chars", "text")}
                         for section in parsed["sections"]],
            "section_hashes": {section["id"]: sha256(section["text"].encode()).hexdigest()
                               for section in parsed["sections"]},
            "boilerplate_blocks": parsed["boilerplate_blocks"],
            "jsonld": parsed["jsonld"],
            "jsonld_valid_through_raw": parsed["jsonld_valid_through_raw"],
            "jsonld_valid_through_mirrors_start_date": (
                parsed["jsonld_valid_through_raw"] is not None
                and parsed["jsonld_valid_through_raw"].replace("/", "-")
                == (parsed["start_date_raw"] or "")[:10]),
            "apply_url": parsed["apply_url"],
            "apply_ref_matches_requisition": parsed["apply_ref"] == parsed["ref"],
            "start_date_raw": parsed["start_date_raw"],
            "start_date_visible": parsed["start_date_visible"],
            "reference_visible": parsed["reference_visible"],
            "visible_location": parsed["visible_location"],
            "mission_preview_matches_primary_section": parsed["mission_preview_matches_primary_section"],
            "description_chars": parsed["description_chars"],
            "description_sha256": parsed["description_sha256"],
            "description_fnv1a64": fnv1a64(normalized_text(parsed["description_text"])),
            "description_normalized_chars": len(normalized_text(parsed["description_text"])),
            "issues": parsed["issues"],
        })
        if role_content_chars < 100:
            job.description_html = ""
            job.description_text = ""
            job.description_unavailable = True
            job.description_method = "societe-generale.detail.placeholder-role-sections"
            job.warnings.append("publisher_placeholder_or_empty_role_sections")
            self.result.errors.append(f"detail_placeholder_sections:{job.job_id}:{url}")
        if parsed["issues"]:
            job.warnings.extend(parsed["issues"])
        if parsed["apply_ref"] != parsed["ref"]:
            job.warnings.append("apply_reference_mismatch")
        if parsed["reference_visible"] and parsed["reference_visible"] != parsed["ref"]:
            job.warnings.append("visible_reference_mismatch")

    # ---- jobs ------------------------------------------------------------
    def _make_job(self, doc, location):
        doc = dict(doc)
        job = self.job(
            {"listing_doc": doc, "detail": {}, "location": location,
             "alternate_language_url": None, "sitemap_listed": None},
            title=normalized_text(doc.get("title")),
            url=doc["resulturl"],
            job_id=str(doc["sourcestr12"]),
            requisition_id=str(doc["sourcestr4"]),
            apply_url=doc.get("url3"),
            description="",
            method=None,
            description_url=None,
            locations=[location["text"]] if location.get("text") else [],
            department=doc.get("sourcestr10"),
            employment_type=doc.get("sourcestr8"),
            workplace_type="Hybrid" if doc.get("sourcebool1") is True else None,
            date_posted=None,
            date_posted_raw={"listing_sourcedatetime1": doc.get("sourcedatetime1")},
            employer=None,
            salary=None,
            language=doc.get("languages"),
        )
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": (
                "Société Générale's own /en/search Quantum/CES listing, requested through "
                "its own /search-proxy.php; no filters, no other company or board."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": PROXY_URL,
                "proxy_target": DEFAULT_PROFILE_URL,
                "body_shape": self._search_body(PAGE_SIZE, 0),
                "filters_applied": False,
                "pagination": "skipCount/skipFrom over the widget's own result set; "
                              "the widget's 10-row page 1 is re-requested as a cross-check.",
            },
            "detail_method": "Exact listing resulturl pages; role sections "
                             "(job-detail-description/profile/evolution/group) captured in full.",
        })
        try:
            if self.initial_snapshot is None:
                entry = await self._entry()
                snapshot = await self._listing_snapshot()
                sitemap = await self._sitemap()
            else:
                entry = dict(self.initial_snapshot["entry"])
                snapshot = dict(self.initial_snapshot["listing"])
                sitemap = dict(self.initial_snapshot.get("sitemap") or {})
                self.settings = self.initial_snapshot["settings"]
                self.result.timings["reused_initial_listing_snapshot_seconds"] = 0.0
                self.result.metadata["reused_initial_listing_snapshot"] = True
        except Exception as exc:
            self.result.errors.append(f"Société Générale source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        docs = snapshot["docs"]
        location_map = self.settings["filters"]["refLocation"]["id"]
        locations = {
            str(doc["sourcestr4"]): location_text(
                str(doc["sourcestr4"]), doc["languages"], self.settings["all_offers"], location_map)
            for doc in docs
        }
        by_ref_languages: dict[str, list[dict]] = {}
        for doc in docs:
            by_ref_languages.setdefault(str(doc["sourcestr4"]), []).append(doc)
        sitemap_refs = set(sitemap.get("requisitions") or [])

        jobs = []
        for doc in docs:
            ref = str(doc["sourcestr4"])
            job = self._make_job(doc, locations[ref])
            alternates = [other["resulturl"] for other in by_ref_languages[ref]
                          if other["sourcestr12"] != doc["sourcestr12"]]
            job.raw["alternate_language_url"] = alternates[0] if alternates else None
            job.raw["sitemap_listed"] = ref in sitemap_refs
            jobs.append(job)

        self.result.expected_total = snapshot["total_count"]
        self.result.metadata.update({
            "initial_official_entry": {key: entry[key] for key in (
                "url", "title", "status", "seconds", "response_sha256", "response_bytes", "profile_url")},
            "initial_listing_snapshot": {key: snapshot[key] for key in (
                "total_count", "page_count", "widget_first_page", "issues", "request_seconds", "membership")},
            "ui_page_count": (snapshot["widget_first_page"] or {}).get("page_count"),
            "initial_listing_pages": snapshot["pages"],
            "listing_filter_options": {
                "contract_types": self.settings["filters"].get("refContrat", {}).get("en", {}),
                "job_families": self.settings["filters"].get("refMetier", {}).get("en", {}),
                "locations_count": len(self.settings["filters"].get("refLocation", {}).get("en", {})),
                "all_offers_count": len(self.settings["all_offers"]),
                "filters_applied": False,
            },
            "sitemap_cross_check": {key: sitemap.get(key) for key in (
                "status", "seconds", "response_sha256", "response_bytes",
                "job_urls", "distinct_requisitions")},
            "sitemap_index_differences": {
                "in_index_not_sitemap": sorted(set(by_ref_languages) - sitemap_refs),
                "in_sitemap_not_index": sorted(sitemap_refs - set(by_ref_languages)),
            },
        })
        if snapshot["issues"]:
            self.result.errors.extend(snapshot["issues"])

        await self.hydrate(jobs, self._detail)
        self.result.jobs = [job for job in jobs if job is not None]
        if len(self.result.jobs) != len(docs):
            self.result.errors.append("retained_rows_do_not_match_listing_rows")

        final = None
        try:
            await self._refresh_session()
            final = await self._listing_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Société Générale final listing recheck failed: {type(exc).__name__}: {exc}")
        if final is not None:
            initial_ids = [doc["sourcestr12"] for doc in docs]
            final_ids = [doc["sourcestr12"] for doc in final["docs"]]
            final_by_id = {doc["sourcestr12"]: doc for doc in final["docs"]}
            changed = {}
            for doc in docs:
                other = final_by_id.get(doc["sourcestr12"])
                if other is None:
                    continue
                fields = sorted(key for key in set(doc) | set(other)
                                if key not in VOLATILE_LISTING_FIELDS
                                and doc.get(key) != other.get(key))
                if fields:
                    changed[doc["sourcestr12"]] = fields
            widget_stable = (
                snapshot["widget_first_page"] is not None
                and final["widget_first_page"] is not None
                and snapshot["widget_first_page"]["docids"] == final["widget_first_page"]["docids"]
            )
            complete = (initial_ids == final_ids and snapshot["total_count"] == final["total_count"]
                        and not changed and not final["issues"] and widget_stable)
            self.result.metadata["final_listing_snapshot"] = {
                key: final[key] for key in ("total_count", "page_count", "issues", "membership")}
            self.result.metadata["membership_reconciliation"] = {
                "initial_rows": len(initial_ids),
                "final_rows": len(final_ids),
                "initial_total_count": snapshot["total_count"],
                "final_total_count": final["total_count"],
                "same_rows_and_order": initial_ids == final_ids,
                "widget_first_page_stable": widget_stable,
                "new_rows": [identity for identity in final_ids if identity not in set(initial_ids)],
                "removed_rows": [identity for identity in initial_ids if identity not in set(final_ids)],
                "volatile_fields_excluded": list(VOLATILE_LISTING_FIELDS),
                "changed_fields_by_row": {key: value for key, value in list(changed.items())[:200]},
                "changed_row_count": len(changed),
                "complete": complete,
            }
            if initial_ids != final_ids:
                self.result.errors.append("listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("listing_source_fields_changed_during_collection")
            if not widget_stable:
                self.result.errors.append("listing_widget_first_page_changed_during_collection")
            self.result.errors.extend(final["issues"])
        else:
            self.result.metadata["membership_reconciliation"] = {"complete": False,
                                                                 "reason": "final_recheck_failed"}

        reconciliation = self.result.metadata["membership_reconciliation"]
        self.result.listing_complete = bool(
            final is not None and reconciliation.get("complete")
            and len(self.result.jobs) == snapshot["total_count"]
        )
        self.result.metadata["detail_outcomes"] = {
            "fetched_live": sum(1 for job in jobs
                                if job.raw.get("detail", {}).get("status") == 200
                                and not job.raw.get("detail", {}).get("reused_cached_response")),
            "reused_cached": sum(1 for job in jobs
                                 if job.raw.get("detail", {}).get("reused_cached_response")),
            "unavailable_410": sum(1 for job in jobs if job.description_unavailable),
            "verified": sum(1 for job in jobs if job.description_verified),
        }
        self.result.metadata["membership"] = snapshot["membership"]
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6)
        return self.result
