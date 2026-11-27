"""Cognizant's official server-rendered global careers listing and details."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import gzip
from hashlib import sha256
from html import escape
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
import httpx

from ..attachments import extract_attachment
from ..http import FetchError
from ..models import now, text_html
from .base import Adapter


ENTRY = "https://careers.cognizant.com/global-en/jobs/"
HOST = "careers.cognizant.com"
PAGE_SIZE = 10
RENDERED_SOURCE_CODE = "CWS-13082"
NATIVE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; company-careers-audit/1.0)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
}
JOB_PATH = re.compile(r"^/global-en/jobs/(\d+)/([^/]+)/?$")
DISPLAYING = re.compile(
    r"Displaying\s+([\d,]+)\s+to\s+([\d,]+)\s+of\s+([\d,]+)\s+matching jobs",
    re.I,
)
DEADLINE = re.compile(
    r"applications? (?:will be )?accepted until\s+([A-Za-z]+\s+\d{1,2}\s*(?:st|nd|rd|th)?\s*,?\s+\d{4})",
    re.I,
)
MONEY_RANGE = re.compile(
    r"(?:annual salary|salary)[^.$\n]{0,120}?(?:between\s+)?(?:(CAD|USD|INR|GBP|EUR|AUD|SGD)\s*)?\$?\s*([\d,]+)\s*(?:-|to|and)\s*(?:(CAD|USD|INR|GBP|EUR|AUD|SGD)\s*)?\$?\s*([\d,]+)",
    re.I,
)


def _text(value):
    return " ".join((value or "").split())


def _decode_cloudflare_emails(article):
    """Render the exact public email text Cloudflare protects in source HTML."""
    for span in article.select("span.__cf_email__[data-cfemail]"):
        encoded = span.get("data-cfemail", "")
        if len(encoded) < 4 or len(encoded) % 2:
            continue
        try:
            key = int(encoded[:2], 16)
            decoded = "".join(chr(int(encoded[index:index + 2], 16) ^ key)
                              for index in range(2, len(encoded), 2))
        except (ValueError, UnicodeDecodeError):
            continue
        span.clear()
        span.append(decoded)
    return article


def _rendered_inner_text(node):
    """Approximate browser innerText while keeping inline punctuation adjacent."""
    block = {"address", "article", "blockquote", "body", "dd", "div", "dl", "dt",
             "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
             "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
             "section", "table", "tr", "ul"}

    def visit(item):
        if isinstance(item, NavigableString):
            return str(item)
        if not isinstance(item, Tag):
            return ""
        if (item.name in {"script", "style", "noscript"} or item.has_attr("hidden")
                or item.get("aria-hidden", "").casefold() == "true"):
            return ""
        if item.name == "br":
            return "\n"
        content = "".join(visit(child) for child in item.children)
        return f"\n{content}\n" if item.name in block else content

    return "\n".join(line.strip() for line in visit(node).splitlines() if line.strip())


def _integer(value):
    return int(value.replace(",", ""))


def _fnv1a64(value):
    result = 14695981039346656037
    for character in _text(value):
        result = ((result ^ ord(character)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{result:016x}"


def _official_job_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.hostname == HOST
            and JOB_PATH.fullmatch(parsed.path) is not None)


def _job_identifier(value):
    if isinstance(value, dict):
        return value.get("value") or value.get("identifier") or value.get("name")
    return value


def _job_posting(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "JobPosting":
                return item
            graph = item.get("@graph")
            if isinstance(graph, list):
                match = next((entry for entry in graph
                              if isinstance(entry, dict) and entry.get("@type") == "JobPosting"), None)
                if match:
                    return match
    raise ValueError("Cognizant detail page has no JobPosting JSON-LD object")


def _summary(text):
    match = DISPLAYING.search(text)
    if not match:
        raise ValueError("Cognizant listing page has no visible 'Displaying X to Y of Z' summary")
    return tuple(_integer(value) for value in match.groups())


def _page_number_from_href(href):
    values = parse_qs(urlsplit(href).query).get("page")
    return _integer(values[0]) if values and values[0].isdigit() else 1


def _append_rendered_apply_tracking(raw_url):
    """Mirror the exact first-party app.js referrer behavior for Taleo links."""
    parsed = urlsplit(raw_url)
    if not parsed.hostname or not parsed.hostname.endswith(".taleo.net"):
        return raw_url, None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "source" for key, _ in query):
        source = next((value for key, value in query if key == "source"), None)
        if source and not any(key == "src" for key, _ in query):
            query.append(("src", source))
    else:
        source = RENDERED_SOURCE_CODE
        query.extend((("source", source), ("src", source)))
    rendered = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return rendered, source


def _page_sections(article):
    """Split the complete visible CMS article at its source-marked headings."""
    sections = []
    current = {"heading": None, "html": []}
    heading_names = {
        "about the role", "job summary", "in this role, you will:",
        "in this role, you will", "about the role", "work model",
        "work authorization", "what you need to have to be considered",
        "these will help you stand out", "required qualifications",
        "preferred qualifications", "qualifications", "responsibilities",
        "salary transparency (canada)", "salary transparency (united states)",
        "salary and other compensation", "salary and other compensation:",
        "additional employment information", "about cognizant:",
        "about cognizant",
    }
    for child in article.children:
        if not isinstance(child, Tag):
            continue
        label = None
        if child.name == "p":
            strongs = child.find_all("strong", recursive=False)
            text = _text(child.get_text(" ", strip=True))
            if len(strongs) == 1 and _text(strongs[0].get_text(" ", strip=True)) == text:
                candidate = text.casefold()
                if candidate in heading_names:
                    label = text.rstrip(":")
        if label:
            if current["heading"] is not None or current["html"]:
                sections.append(current)
            current = {"heading": label, "html": []}
        else:
            current["html"].append(str(child))
    if current["heading"] is not None or current["html"]:
        sections.append(current)
    return [
        {"heading": section["heading"],
         "html": "".join(section["html"]),
         "text": _rendered_inner_text(BeautifulSoup("".join(section["html"]), "lxml"))}
        for section in sections
    ]


def _salary_from_description(sections, jsonld_salary):
    if jsonld_salary:
        return jsonld_salary, None
    for section in sections:
        if not (section["heading"] or "").casefold().startswith("salary"):
            continue
        source = _text(section["text"])
        match = MONEY_RANGE.search(source)
        if not match:
            return None, source
        currency_a, minimum, currency_b, maximum = match.groups()
        period = "year" if re.search(r"annual|per year|yearly", source, re.I) else None
        return {
            "minimum": _integer(minimum),
            "maximum": _integer(maximum),
            "currency": currency_a or currency_b,
            "period": period,
            "source_text": source,
        }, source
    return None, None


def _valid_through(jsonld, description_text):
    if jsonld.get("validThrough"):
        return jsonld["validThrough"], None
    match = DEADLINE.search(description_text)
    if not match:
        return None, None
    raw = match.group(1)
    clean = re.sub(r"(\d)\s*(st|nd|rd|th)", r"\1", raw, flags=re.I).replace(",", "")
    clean = _text(clean)
    try:
        parsed = datetime.strptime(clean, "%B %d %Y").date().isoformat()
    except ValueError:
        return None, raw
    return parsed, raw


def _role_attachments(article, detail_url):
    found = []
    suffixes = {".pdf", ".doc", ".docx", ".rtf", ".txt", ".odt"}
    for anchor in article.select("a[href]"):
        target = urljoin(detail_url, anchor["href"])
        suffix = Path(urlsplit(target).path).suffix.lower()
        if suffix in suffixes and urlsplit(target).scheme in {"https", "http"}:
            found.append({"label": _text(anchor.get_text(" ", strip=True)), "url": target,
                          "filename": Path(urlsplit(target).path).name})
    return found


class Cognizant(Adapter):
    VERIFICATION_REVISION = "cognizant.official-global-en-html-pagination-jsonld.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.research_reuse_enabled = False
        self.research_capture_root = Path(__file__).resolve().parents[3] / "companies" / "cognizant" / "source-captures"
        self.research_page_captures = {}
        self.research_detail_captures = {}

    def enable_research_reuse(self):
        """Reuse the already captured exact first, second, terminal, and sample detail bodies."""
        page1_path = self.research_capture_root / "official-entry-provenance.json"
        pages_path = self.research_capture_root / "official-pagination-pages-provenance.json"
        details_path = self.research_capture_root / "official-details-provenance.json"
        if page1_path.exists():
            page1 = json.loads(page1_path.read_text(encoding="utf-8"))
            if page1.get("status") == 200 and page1.get("sha256") and page1.get("body_gzip"):
                self.research_page_captures[1] = {
                    **page1, "request_url": ENTRY,
                    "source_provenance": "companies/cognizant/source-captures/official-entry-provenance.json",
                }
        if pages_path.exists():
            page_bundle = json.loads(pages_path.read_text(encoding="utf-8"))
            for record in page_bundle.get("requests", []):
                page = _page_number_from_href(record.get("published_url", ""))
                if page > 1 and record.get("status") == 200 and record.get("sha256") and record.get("body_gzip"):
                    self.research_page_captures[page] = {
                        **record,
                        "source_provenance": "companies/cognizant/source-captures/official-pagination-pages-provenance.json",
                    }
        if details_path.exists():
            detail_bundle = json.loads(details_path.read_text(encoding="utf-8"))
            for record in detail_bundle.get("requests", []):
                if record.get("status") == 200 and record.get("url") and record.get("sha256") and record.get("raw_body_gzip"):
                    self.research_detail_captures[record["url"]] = {
                        **record,
                        "source_provenance": "companies/cognizant/source-captures/official-details-provenance.json",
                    }
        self.research_reuse_enabled = True

    def _read_research_body(self, record):
        filename = record.get("body_gzip") or record.get("raw_body_gzip")
        if not filename:
            raise ValueError("Prior Cognizant capture has no raw body file name")
        path = self.research_capture_root / filename
        raw = gzip.decompress(path.read_bytes())
        digest = sha256(raw).hexdigest()
        if digest != record.get("sha256"):
            raise ValueError(f"Prior Cognizant raw response hash mismatch: {filename}")
        return raw, path

    def _archive_reused_response(self, url, record, raw, raw_path, *, purpose):
        fetched_at = record.get("fetched_at")
        if not fetched_at:
            fetched_at = datetime.fromtimestamp(raw_path.stat().st_mtime, timezone.utc).isoformat()
        if self.http.output:
            response_path = self.http.output / "responses" / f"{record['sha256']}.gz"
            if not response_path.exists():
                response_path.write_bytes(gzip.compress(raw, mtime=0))
            request_record = {
                "url": url.split("#", 1)[0], "final_url": record.get("final_url") or url.split("#", 1)[0],
                "method": "GET", "request_body": None, "status": record.get("status", 200),
                "sha256": record["sha256"], "content_type": record.get("content_type"),
                "redirects": record.get("redirects", []), "fetched_at": fetched_at,
                "reused": True, "reuse_purpose": purpose,
                "source_capture": record.get("source_provenance"),
                "original_request_duration_seconds": record.get("duration_seconds"),
            }
            with (self.http.output / "requests.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(request_record, ensure_ascii=False) + "\n")
        return fetched_at

    async def _listing_page(self, page_number, *, initial=False):
        if not self.page_guard():
            return None, {"page": page_number, "outcome": "page_budget_exhausted"}
        url = ENTRY if page_number == 1 else urljoin(ENTRY, f"?page={page_number}#results")
        reused = self.research_page_captures.get(page_number) if self.research_reuse_enabled else None
        if reused:
            raw, raw_path = self._read_research_body(reused)
            request_url = (reused.get("request_url") or reused.get("url") or url).split("#", 1)[0]
            response = httpx.Response(200, content=raw, request=httpx.Request("GET", request_url))
            elapsed = self._archive_reused_response(url, reused, raw, raw_path, purpose="official_listing_page")
            record = {
                "page": page_number, "url": url, "final_url": str(response.url), "status": 200,
                "sha256": reused["sha256"], "redirects": [], "duration_seconds": 0.0,
                "reused": True, "reused_fetched_at": elapsed,
                "original_request_duration_seconds": reused.get("duration_seconds"),
                "source_capture": reused.get("source_provenance"),
                "purpose": "official_global_listing_page_reused_from_initial_review",
            }
            self.result.metadata.setdefault("listing_pages", []).append(record)
            reused_list = self.result.metadata.setdefault("reused_listing_pages", [])
            reused_list.append({"page": page_number, "sha256": reused["sha256"],
                                "source_capture": reused.get("source_provenance"),
                                "original_request_duration_seconds": reused.get("duration_seconds")})
            return response, record
        started = time.perf_counter()
        try:
            response = await self.http.get(url, headers=NATIVE_HEADERS)
        except FetchError as exc:
            record = {"page": page_number, "url": url, "outcome": "request_failed",
                      "error": str(exc), "duration_seconds": max(0.0, time.perf_counter() - started)}
            self.result.metadata.setdefault("listing_pages", []).append(record)
            return None, record
        record = {
            "page": page_number, "url": url, "final_url": str(response.url),
            "status": response.status_code, "sha256": sha256(response.content).hexdigest(),
            "redirects": [str(item.url) for item in response.history],
            "duration_seconds": max(0.0, time.perf_counter() - started),
            "purpose": "official_global_unfiltered_listing_page" if not initial else "official_global_listing_page_1",
        }
        self.result.metadata.setdefault("listing_pages", []).append(record)
        return response, record

    async def _refetch_listing_page(self, page_number, reason, *, total, page_size, total_pages):
        """Re-request one exact official listing page once after a detected anomaly.

        Cognizant occasionally returns a page whose summary is correct but whose
        cards belong to a distant page, or whose last-page link points at the
        wrong page. The anomalous response is retained as evidence; the retry is
        used only when its own contract and card count are internally valid.
        """
        url = ENTRY if page_number == 1 else urljoin(ENTRY, f"?page={page_number}#results")
        started = time.perf_counter()
        try:
            response = await self.http.get(url, headers=NATIVE_HEADERS)
        except FetchError as exc:
            self.result.metadata.setdefault("listing_page_retries", []).append({
                "page": page_number, "url": url, "reason": reason,
                "outcome": "retry_request_failed", "error": str(exc),
                "chosen": "original",
                "duration_seconds": max(0.0, time.perf_counter() - started),
            })
            return None
        record = {
            "page": page_number, "url": url, "final_url": str(response.url),
            "status": response.status_code, "sha256": sha256(response.content).hexdigest(),
            "redirects": [str(item.url) for item in response.history],
            "duration_seconds": max(0.0, time.perf_counter() - started),
            "purpose": "official_global_listing_page_anomaly_retry",
        }
        self.result.metadata.setdefault("listing_pages", []).append(record)
        parsed = urlsplit(str(response.url))
        if (parsed.hostname != HOST or parsed.path.rstrip("/") != "/global-en/jobs"
                or _page_number_from_href(str(response.url)) != page_number):
            self.result.metadata.setdefault("listing_page_retries", []).append({
                "page": page_number, "url": url, "reason": reason,
                "retry_sha256": record["sha256"],
                "outcome": "retry_resolved_to_unexpected_url", "chosen": "original",
            })
            return None
        soup = BeautifulSoup(response.text, "lxml")
        try:
            contract = self._page_contract(soup, response, page_number)
            cards, errors = self._page_cards(soup, page_number)
        except ValueError as exc:
            self.result.metadata.setdefault("listing_page_retries", []).append({
                "page": page_number, "url": url, "reason": reason,
                "retry_sha256": record["sha256"],
                "outcome": "retry_parse_failed", "error": str(exc), "chosen": "original",
            })
            return None
        contract_matches = (
            contract["total"] == total and contract["page_size"] == page_size
            and contract["total_pages"] == total_pages
        )
        expected_count = contract["end"] - contract["start"] + 1
        self.result.metadata.setdefault("listing_page_retries", []).append({
            "page": page_number, "url": url, "reason": reason,
            "retry_sha256": record["sha256"],
            "retry_ids": [card["id"] for card in cards],
            "retry_contract_matches_first_page": contract_matches,
            "retry_card_count_matches_summary": len(cards) == expected_count,
            "retry_card_errors": errors,
        })
        return response, record, contract, cards, errors

    def _page_contract(self, soup, response, page_number):
        text = _text(soup.get_text(" ", strip=True))
        start, end, total = _summary(text)
        form = soup.select_one("form#job-filter")
        if form is None or (form.get("method") or "").casefold() != "get":
            raise ValueError("Cognizant native GET job filter form is missing or changed")
        action = urljoin(str(response.url), form.get("action") or str(response.url))
        if urlsplit(action).hostname != HOST or urlsplit(action).path.rstrip("/") != "/global-en/jobs":
            raise ValueError("Cognizant listing form no longer targets the observed official jobs page")
        page_size = None
        for control in form.select('input[name="pagesize"]'):
            if control.get("value"):
                page_size = int(control["value"])
                break
        if not page_size:
            raise ValueError("Cognizant global listing page size is missing")
        pager = soup.select_one('a.page-link[rel~="last"][href]')
        if pager:
            last_page = _page_number_from_href(pager["href"])
        else:
            last_page = math.ceil(total / page_size)
        expected_start = (page_number - 1) * page_size + 1
        expected_end = min(page_number * page_size, total)
        if start != expected_start or end != expected_end:
            raise ValueError(
                f"Cognizant page {page_number} summary {start}-{end}/{total} differs from native page-size {page_size} expectation"
            )
        expected_last = math.ceil(total / page_size)
        if last_page != expected_last:
            raise ValueError(f"Cognizant visible last page {last_page} does not match total/page-size {expected_last}")
        filters = []
        for control in form.select("input[name],select[name]"):
            if control.name == "input":
                filters.append({"name": control.get("name"), "type": control.get("type"),
                                "value": control.get("value")})
            else:
                filters.append({"name": control.get("name"), "options": [
                    {"value": option.get("value"), "label": _text(option.get_text(" ", strip=True))}
                    for option in control.select("option")
                ]})
        return {"start": start, "end": end, "total": total, "page_size": page_size,
                "total_pages": last_page, "form_action": action, "form_method": "GET",
                "filters": filters, "summary_text": f"Displaying {start} to {end} of {total} matching jobs"}

    def _page_cards(self, soup, page_number):
        cards = []
        errors = []
        for card in soup.select(".card.card-job"):
            anchor = card.select_one("h2.card-title a[href]") or card.select_one("h2 a[href]")
            job_id = card.get("data-id")
            if anchor is None:
                errors.append(f"cognizant_page_{page_number}_card_missing_job_link")
                continue
            url = urljoin(ENTRY, anchor["href"])
            path_match = JOB_PATH.fullmatch(urlsplit(url).path)
            linked_id = path_match.group(1) if path_match else None
            if not job_id or not path_match or urlsplit(url).hostname != HOST:
                errors.append(f"cognizant_page_{page_number}_card_has_invalid_company_job_identity:{url}")
                continue
            if str(job_id) != str(linked_id):
                errors.append(f"cognizant_page_{page_number}_card_data_id_does_not_match_link:{job_id}:{linked_id}")
                continue
            title = _text(anchor.get_text(" ", strip=True))
            card_meta = [_text(item.get_text(" ", strip=True)) for item in card.select(".job-meta li")]
            cards.append({"id": str(job_id), "title": title, "url": url,
                          "metadata": card_meta, "page": page_number})
        return cards, errors

    def _official_entry_metadata(self, soup, first_contract, last_page_anchor):
        regions = [
            {"label": _text(anchor.get_text(" ", strip=True)), "url": urljoin(ENTRY, anchor["href"])}
            for anchor in soup.select("a[href]")
            if _text(anchor.get_text(" ", strip=True))
            and any(token in _text(anchor.get_text(" ", strip=True)).casefold()
                    for token in ("india", "united states", "all other countries", "canada", "global site"))
        ]
        return {
            "official_entry": ENTRY,
            "global_listing_url": str(urljoin(ENTRY, "")),
            "scope": "Cognizant global-en unfiltered listings; all 10 visible entries include worldwide regions",
            "first_page_total": first_contract["total"],
            "page_size": first_contract["page_size"],
            "total_pages": first_contract["total_pages"],
            "filter_form_method": first_contract["form_method"],
            "filter_form_action": first_contract["form_action"],
            "filter_controls": first_contract["filters"],
            "native_last_page_link": last_page_anchor,
            "regional_navigation_links_observed_not_enumerated": regions,
            "saved_and_related_job_api_calls_used_for_membership": False,
            "ats_board_requests": 0,
        }

    async def _detail(self, job):
        capture = self.research_detail_captures.get(job.url) if self.research_reuse_enabled else None
        reused = capture is not None
        request_elapsed = 0.0
        reused_fetched_at = None
        if reused:
            raw, raw_path = self._read_research_body(capture)
            response = httpx.Response(200, content=raw, request=httpx.Request("GET", job.url))
            reused_fetched_at = self._archive_reused_response(
                job.url, capture, raw, raw_path, purpose="exact_listed_job_detail")
            self.result.metadata.setdefault("reused_detail_records", []).append({
                "job_id": job.job_id, "url": job.url, "sha256": capture["sha256"],
                "fetched_at": reused_fetched_at, "source_capture": capture.get("source_provenance"),
                "original_request_duration_seconds": capture.get("duration_seconds"),
            })
        else:
            started = time.perf_counter()
            try:
                response = await self.http.get(job.url, headers=NATIVE_HEADERS)
            except FetchError as exc:
                job.raw["detail_fetch_error"] = str(exc)
                self.result.timings["detail_request_seconds"] = (
                    self.result.timings.get("detail_request_seconds", 0.0)
                    + max(0.0, time.perf_counter() - started)
                )
                raise
            request_elapsed = max(0.0, time.perf_counter() - started)
            self.result.timings["detail_request_seconds"] = (
                self.result.timings.get("detail_request_seconds", 0.0) + request_elapsed
            )
        parse_started = time.perf_counter()
        response_hash = sha256(response.content).hexdigest()
        soup = BeautifulSoup(response.text, "lxml")
        structured = _job_posting(soup)
        source_id = str(_job_identifier(structured.get("identifier")) or "")
        source_title = _text(structured.get("title"))
        source_url = structured.get("url")
        if source_id != job.job_id:
            raise ValueError(f"JobPosting identifier {source_id!r} does not match listed job id {job.job_id!r}")
        if source_title != job.title:
            raise ValueError(f"JobPosting title {source_title!r} does not match listing title {job.title!r}")
        linked_source = urlsplit(urljoin(str(response.url), source_url or ""))
        requested_job = urlsplit(job.url)
        if (not source_url or linked_source.scheme != "https" or linked_source.hostname != HOST
                or linked_source.path != requested_job.path):
            raise ValueError("JobPosting URL does not match the exact official listing detail URL")
        organization = structured.get("hiringOrganization")
        org_name = organization.get("name") if isinstance(organization, dict) else None
        if org_name != "Cognizant":
            raise ValueError(f"JobPosting hiringOrganization is not Cognizant: {org_name!r}")
        article = soup.select_one("article.cms-content")
        if article is None:
            raise ValueError("Cognizant official detail page lacks its full cms-content role article")
        raw_article_html = "".join(str(child) for child in article.contents)
        rendered_article = _decode_cloudflare_emails(deepcopy(article))
        article_html = "".join(str(child) for child in rendered_article.contents)
        article_text = _rendered_inner_text(rendered_article)
        if len(article_text.strip()) < 100:
            raise ValueError("Cognizant role article is too short to represent full details")
        jsonld_description = structured.get("description")
        if not isinstance(jsonld_description, str) or len(text_html(jsonld_description).strip()) < 100:
            raise ValueError("Cognizant JobPosting JSON-LD lacks a full description field")
        header = soup.select_one(".text-wrapper")
        header_fields = {}
        if header:
            for item in header.select(".job-meta-item"):
                label = _text(item.get_text(" ", strip=True))
                for key in ("Date published", "Location", "Job category", "Work model"):
                    if label.casefold().startswith(key.casefold()):
                        value = _text(item.get_text(" ", strip=True)[len(key):])
                        header_fields[key] = value
                        break
        sections = _page_sections(rendered_article)
        responsibility_sections = [section["text"] for section in sections
                                   if (section["heading"] or "").casefold() in
                                   {"in this role, you will", "job summary", "about the role"} and section["text"]]
        qualification_sections = [section["text"] for section in sections
                                  if (section["heading"] or "").casefold() in
                                  {"what you need to have to be considered", "these will help you stand out",
                                   "required qualifications", "preferred qualifications", "qualifications"}
                                  and section["text"]]
        salary, salary_text = _salary_from_description(sections, structured.get("baseSalary"))
        jsonld_text = text_html(jsonld_description)
        valid_through, valid_through_raw = _valid_through(structured, jsonld_text)
        rendered_apply = next((urljoin(str(response.url), anchor["href"])
                               for anchor in soup.select("a.js-apply-now[href]")
                               if _text(anchor.get_text(" ", strip=True)).casefold() == "apply now"), None)
        if not rendered_apply:
            raise ValueError("Cognizant published detail page has no exact Apply Now link")
        apply_url, source_code = _append_rendered_apply_tracking(rendered_apply)
        apply_parts = urlsplit(apply_url)
        apply_query = parse_qs(apply_parts.query)
        if apply_parts.hostname and apply_parts.hostname.endswith(".taleo.net"):
            if apply_query.get("job") != [job.job_id]:
                raise ValueError("Cognizant's exact Taleo apply URL does not carry this listed job ID")
        attachments = _role_attachments(rendered_article, str(response.url))

        job.job_id = str(job.job_id)
        job.requisition_id = source_id
        job.apply_url = apply_url
        if reused_fetched_at:
            job.fetched_at = reused_fetched_at
        job.description_html = article_html
        job.description_text = article_text
        job.description_method = "cognizant.official_article_cms_content"
        job.description_source_url = str(response.url)
        job.date_posted = structured.get("datePosted")
        job.date_posted_raw = structured.get("datePosted")
        job.valid_through = valid_through
        job.employment_type = structured.get("employmentType")
        job.locations = structured.get("jobLocation") if isinstance(structured.get("jobLocation"), list) else ([structured["jobLocation"]] if structured.get("jobLocation") else [])
        job.department = structured.get("industry")
        job.team = structured.get("industry")
        job.workplace_type = header_fields.get("Work model") or structured.get("jobLocationType")
        job.salary = salary
        job.qualifications = qualification_sections or structured.get("educationRequirements")
        job.responsibilities = responsibility_sections or None
        job.education = structured.get("educationRequirements")
        job.experience = structured.get("experienceRequirements")
        job.employer = org_name
        jsonld_metadata = {key: value for key, value in structured.items() if key != "description"}
        job.raw["detail"] = {
            "url": str(response.url),
            "response_status": response.status_code,
            "response_sha256": response_hash,
            "response_archive": f"responses/{response_hash}.gz" if self.http.output else None,
            "duration_seconds": request_elapsed,
            "capture_reused": reused,
            "source_capture": capture.get("source_provenance") if reused else None,
            "original_request_duration_seconds": capture.get("duration_seconds") if reused else request_elapsed,
            "job_posting_jsonld_metadata": jsonld_metadata,
            "jsonld_description_html_sha256": sha256(jsonld_description.encode()).hexdigest(),
            "jsonld_description_html_characters": len(jsonld_description),
            "article_cms_content_raw_html_sha256": sha256(raw_article_html.encode()).hexdigest(),
            "article_cms_content_rendered_html_sha256": sha256(article_html.encode()).hexdigest(),
            "article_text_characters": len(article_text),
            "article_text_fnv1a64": _fnv1a64(article_text),
            "cloudflare_email_addresses_decoded": len(article.select("span.__cf_email__[data-cfemail]")),
            "section_labels": [section["heading"] for section in sections if section["heading"]],
            "visible_header_fields": header_fields,
            "apply_link": {"static_href": rendered_apply,
                           "rendered_href": apply_url,
                           "source_code": source_code,
                           "link_target": "exact listed job only; no Taleo board request"},
            "salary_text": salary_text,
            "valid_through_from_description": valid_through_raw,
            "page_employer_context_is_inside_article": "About Cognizant" in article_text,
            "role_bearing_attachments": [{key: value for key, value in attachment.items()
                                          if key in {"label", "url", "filename"}}
                                         for attachment in attachments],
        }
        downloaded_attachments = []
        for attachment in attachments:
            try:
                file_response = await self.http.get(attachment["url"], headers=NATIVE_HEADERS)
                content = file_response.content
                digest = sha256(content).hexdigest()
                filename = attachment["filename"] or f"{job.job_id}-{digest[:12]}.bin"
                extraction_started = time.perf_counter()
                extracted = await asyncio.to_thread(extract_attachment, content, filename)
                self.result.timings["attachment_extraction_seconds"] = (
                    self.result.timings.get("attachment_extraction_seconds", 0.0)
                    + max(0.0, time.perf_counter() - extraction_started)
                )
                target_path = None
                if self.http.output:
                    target_path = self.http.output / "attachments" / f"{job.job_id}-{digest[:16]}{Path(filename).suffix.lower()}"
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(content)
                attachment_data = {
                    **attachment, "final_url": str(file_response.url),
                    "sha256": digest, "bytes": len(content),
                    "pages": extracted.get("pages"),
                    "output_file": str(target_path.relative_to(self.http.output)) if target_path and self.http.output else None,
                }
                downloaded_attachments.append(attachment_data)
                extracted_text = extracted.get("text", "")
                if not extracted_text.strip():
                    raise ValueError("role-bearing attachment contains no extractable text")
                job.description_html += (
                    "<section><h2>Attached job description: " + escape(filename)
                    + "</h2><pre>" + escape(extracted_text) + "</pre></section>"
                )
                job.description_text += "\nAttached job description\n" + extracted_text
            except Exception as exc:
                job.raw["detail"].setdefault("attachment_errors", []).append(
                    {"url": attachment["url"], "error": f"{type(exc).__name__}: {exc}"}
                )
                job.warnings.append("description_not_full_field")
                job.description_unavailable = True
                self.result.errors.append(f"Cognizant attachment {attachment['url']}: {type(exc).__name__}: {exc}")
        job.raw["detail"]["role_bearing_attachments"] = [
            {key: value for key, value in attachment.items() if key != "text"}
            for attachment in downloaded_attachments
        ]
        if reused:
            job.raw["detail"]["original_capture_fetched_at"] = reused_fetched_at
        parse_duration = max(0.0, time.perf_counter() - parse_started)
        timing_name = "reused_detail_body_parse_seconds" if reused else "detail_parse_seconds"
        self.result.timings[timing_name] = self.result.timings.get(timing_name, 0.0) + parse_duration
        return job

    async def run(self):
        started = time.perf_counter()
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            review_evidence="companies/cognizant/manual-reference.json",
            rendered_reference="companies/cognizant/browser-reference-parent.json",
            company_membership_verified=False,
            global_unfiltered_listing=True,
            expected_company_posting_count=None,
            exact_company_job_ids=[],
            ats_board_requests=0,
            related_or_saved_job_api_requests=0,
            request_profile=NATIVE_HEADERS,
            listing_pages=[],
            listing_page_summaries=[],
            listing_page_retries=[],
            listing_page_ids={},
            detail_failures=[],
        )

        first, first_request = await self._listing_page(1, initial=True)
        self.result.timings["official_entry_listing_seconds"] = first_request.get("duration_seconds", 0.0)
        if first is None:
            self.result.errors.append("cognizant_official_global_listing_page_1_unavailable")
            return self._finish(started)
        if urlsplit(str(first.url)).hostname != HOST or urlsplit(str(first.url)).path.rstrip("/") != "/global-en/jobs":
            self.result.errors.append("cognizant_official_global_listing_destination_changed")
            return self._finish(started)
        first_soup = BeautifulSoup(first.text, "lxml")
        try:
            contract = self._page_contract(first_soup, first, 1)
        except ValueError as exc:
            self.result.errors.append(str(exc))
            return self._finish(started)
        self.result.metadata["listing_contract"] = contract
        total = contract["total"]
        page_size = contract["page_size"]
        total_pages = contract["total_pages"]
        last_link = first_soup.select_one('a.page-link[rel~="last"][href]')
        self.result.metadata["official_last_page_link"] = urljoin(str(first.url), last_link["href"]) if last_link else None
        self.result.metadata.update(self._official_entry_metadata(
            first_soup, contract, self.result.metadata["official_last_page_link"]))
        if urlsplit(str(first.url)).query:
            self.result.errors.append("cognizant_official_entry_has_unexpected_active_filter_query")
        first_cards, card_errors = self._page_cards(first_soup, 1)
        self.result.errors.extend(card_errors)
        ids = set()
        page_ids = {}

        def add_page(page_number, response, cards):
            seen_here = set()
            for card in cards:
                job_id = card["id"]
                if job_id in seen_here:
                    self.result.errors.append(f"cognizant_page_{page_number}_repeats_job_id:{job_id}")
                    continue
                seen_here.add(job_id)
                if job_id in ids:
                    self.result.errors.append(f"cognizant_listing_duplicate_job_id:{job_id}")
                    continue
                ids.add(job_id)
                self.add(self.job(
                    {"listing": {**card, "page_response_sha256": sha256(response.content).hexdigest()}},
                    title=card["title"], job_id=job_id, url=card["url"],
                    record_identity=sha256(f"cognizant|global-en|{job_id}|{card['url']}".encode()).hexdigest(),
                ))
                if self.limit():
                    return False
            page_ids[str(page_number)] = [card["id"] for card in cards]
            return True

        self.result.metadata["listing_page_summaries"].append({"page": 1, **contract})
        if not add_page(1, first, first_cards):
            total_pages = 1
        listing_loop_started = time.perf_counter()
        for page_number in range(2, total_pages + 1):
            if self.limit():
                break
            response, request_record = await self._listing_page(page_number)
            if response is None:
                self.result.errors.append(f"cognizant_listing_page_{page_number}_unavailable")
                if self.result.pages >= self.options.max_pages:
                    break
                continue
            parsed_response_url = urlsplit(str(response.url))
            if (parsed_response_url.hostname != HOST or parsed_response_url.path.rstrip("/") != "/global-en/jobs"
                    or _page_number_from_href(str(response.url)) != page_number):
                self.result.errors.append(f"cognizant_page_{page_number}_resolved_to_unexpected_listing_url:{response.url}")
                continue
            soup = BeautifulSoup(response.text, "lxml")
            contract_error = None
            try:
                page_contract = self._page_contract(soup, response, page_number)
                cards, errors = self._page_cards(soup, page_number)
            except ValueError as exc:
                contract_error = str(exc)
                page_contract, cards, errors = None, [], []
            original_sha = request_record.get("sha256")
            original_cards = [card["id"] for card in cards]
            overlap = [card["id"] for card in cards if card["id"] in ids]

            def page_valid(contract, page_cards, page_errors):
                if contract is None or page_errors:
                    return False
                if (contract["total"] != total or contract["page_size"] != page_size
                        or contract["total_pages"] != total_pages):
                    return False
                expected = contract["end"] - contract["start"] + 1
                return len(page_cards) == expected

            reasons = []
            if contract_error:
                reasons.append("contract_parse_error")
            elif page_contract is None:
                reasons.append("contract_missing")
            else:
                if (page_contract["total"] != total or page_contract["page_size"] != page_size
                        or page_contract["total_pages"] != total_pages):
                    reasons.append("contract_changed")
                if len(cards) != page_contract["end"] - page_contract["start"] + 1:
                    reasons.append("card_count_mismatch")
                if errors:
                    reasons.append("card_errors")
            if overlap:
                reasons.append("duplicate_ids")
            if reasons:
                retry = await self._refetch_listing_page(
                    page_number, "+".join(reasons), total=total,
                    page_size=page_size, total_pages=total_pages)
                if retry is not None:
                    r_response, r_record, r_contract, r_cards, r_errors = retry
                    r_overlap = [card["id"] for card in r_cards if card["id"] in ids]
                    current_ok = page_valid(page_contract, cards, errors)
                    retry_ok = page_valid(r_contract, r_cards, r_errors)
                    entry = self.result.metadata["listing_page_retries"][-1]
                    entry.update({
                        "original_sha256": original_sha,
                        "original_ids": original_cards,
                        "original_overlap_with_collected": overlap,
                        "retry_overlap_with_collected": r_overlap,
                    })
                    if retry_ok and (not current_ok or len(r_overlap) < len(overlap)):
                        entry["chosen"] = "retry"
                        response, request_record, page_contract, cards, errors = (
                            r_response, r_record, r_contract, r_cards, r_errors)
                        overlap = r_overlap
                        contract_error = None
                    else:
                        entry["chosen"] = "original"
            if page_contract is None:
                self.result.errors.append(f"page {page_number}: {contract_error or 'invalid listing contract'}")
                continue
            if (page_contract["total"] != total or page_contract["page_size"] != page_size
                    or page_contract["total_pages"] != total_pages):
                self.result.errors.append(f"cognizant_listing_contract_changed_on_page_{page_number}")
            self.result.errors.extend(errors)
            expected_count = page_contract["end"] - page_contract["start"] + 1
            if len(cards) != expected_count:
                self.result.errors.append(f"cognizant_page_{page_number}_card_count_{len(cards)}_does_not_match_summary_{expected_count}")
            self.result.metadata["listing_page_summaries"].append({"page": page_number, **page_contract,
                                                                    "sha256": request_record.get("sha256")})
            if not add_page(page_number, response, cards):
                break
        self.result.timings["initial_listing_seconds"] = max(0.0, time.perf_counter() - listing_loop_started)
        self.result.metadata["listing_page_ids"] = page_ids
        self.result.metadata["exact_company_job_ids"] = sorted(ids)
        self.result.expected_total = total
        self.result.metadata["observed_page_count"] = len(page_ids)
        self.result.metadata["observed_unique_job_ids"] = len(ids)
        self.result.metadata["company_membership_verified"] = (
            len(page_ids) == total_pages and len(ids) == total and not self.result.errors
        )
        self.result.listing_complete = self.result.metadata["company_membership_verified"]
        if not self.result.listing_complete and "sample_limit" not in self.result.warnings:
            self.result.errors.append("cognizant_global_listing_pages_or_ids_not_fully_reconciled")
        if self.result.jobs:
            self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
            detail_stats = self.result.metadata.get("details", {})
            reused_details = self.result.metadata.get("reused_detail_records", [])
            detail_stats["reused_source_capture_hits"] = len(reused_details)
            detail_stats["network_fetched_jobs"] = max(0, detail_stats.get("fetched_jobs", 0) - len(reused_details))
            self.result.timings["reused_detail_original_request_seconds"] = sum(
                record.get("original_request_duration_seconds") or 0.0 for record in reused_details
            )
            self.result.metadata["detail_failures"] = [
                {"job_id": job.job_id, "title": job.title, "url": job.url,
                 "warnings": job.warnings}
                for job in self.result.jobs if not job.description_verified
            ]
        else:
            self.result.metadata["details"] = {"total": 0, "checked": 0, "fetched_jobs": 0,
                                                "verified_cache_hits": 0, "unavailable_cache_hits": 0,
                                                "failed": 0, "duration_seconds": 0.0}
            self.result.timings["detail_phase_seconds"] = 0.0
        if self.result.metadata["company_membership_verified"]:
            await self._recheck_membership(total_pages, page_ids, total, page_size)
        self.result.metadata["collection_outcome"] = "complete" if self.result.listing_complete and not self.result.errors else "partial"
        self.result.metadata["raw_detail_count"] = len(self.result.jobs)
        return self._finish(started)

    async def _recheck_membership(self, total_pages, original_page_ids, total, page_size):
        started = time.perf_counter()
        mismatches = []
        checks = []
        # The complete 209-page pass already reconciles every ID. Recheck the
        # independently rendered first, second and terminal pages once after
        # detail hydration; do not repeat the full successful global listing.
        sample_pages = sorted({1, min(2, total_pages), total_pages})
        for page_number in sample_pages:
            url = ENTRY if page_number == 1 else urljoin(ENTRY, f"?page={page_number}#results")
            request_started = time.perf_counter()
            try:
                response = await self.http.get(url, headers=NATIVE_HEADERS)
            except FetchError as exc:
                checks.append({"page": page_number, "url": url, "error": str(exc)})
                mismatches.append(page_number)
                continue
            parsed_url = urlsplit(str(response.url))
            if (parsed_url.hostname != HOST or parsed_url.path.rstrip("/") != "/global-en/jobs"
                    or _page_number_from_href(str(response.url)) != page_number):
                checks.append({"page": page_number, "url": url,
                               "error": f"unexpected final URL {response.url}"})
                mismatches.append(page_number)
                continue
            soup = BeautifulSoup(response.text, "lxml")
            try:
                contract = self._page_contract(soup, response, page_number)
                cards, card_errors = self._page_cards(soup, page_number)
            except ValueError as exc:
                checks.append({"page": page_number, "url": url, "error": str(exc)})
                mismatches.append(page_number)
                continue
            current_ids = [card["id"] for card in cards]
            old_ids = original_page_ids.get(str(page_number), [])
            if current_ids != old_ids or contract["total"] != total or contract["page_size"] != page_size:
                mismatches.append(page_number)
            if card_errors:
                mismatches.append(page_number)
            checks.append({"page": page_number, "url": url, "status": response.status_code,
                           "sha256": sha256(response.content).hexdigest(),
                           "duration_seconds": max(0.0, time.perf_counter() - request_started),
                           "summary": contract["summary_text"], "ids": current_ids})
        self.result.timings["final_membership_recheck_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["final_membership_recheck"] = {
            "pages_checked": len(checks), "sampled_pages": sample_pages, "page_count": total_pages,
            "sampled_job_ids": sum(len(item.get("ids", [])) for item in checks),
            "ids_match_initial_snapshot": not mismatches,
            "mismatched_pages": sorted(set(mismatches)), "pages": checks,
        }
        if mismatches:
            self.result.listing_complete = False
            self.result.metadata["company_membership_verified"] = False
            self.result.errors.append("cognizant_global_listing_membership_changed_during_detail_collection")

    def _finish(self, started):
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["company_collection_seconds"] = elapsed
        self.result.timings["source_collection_seconds"] = elapsed
        self.result.duration_seconds = elapsed
        self.result.finished_at = now()
        return self.result
