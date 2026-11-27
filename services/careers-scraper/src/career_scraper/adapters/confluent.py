"""Confluent's published journey: domain 301 -> IBM careers search -> IBM JobDetail pages.

Confluent's own careers URLs (careers.confluent.io/jobs, careers.confluent.io/,
www.confluent.io/careers/) all 301-redirect to
``https://www.ibm.com/careers/search?q=confluent`` because Confluent was
acquired by IBM and its hiring now runs through IBM's board. The listing is
IBM's scoped careers search (``appid=careers``, ``scopeid=careers2``) with the
keyword query ``q=confluent``. Membership is exactly the jobIds that search
exposes; only those exact JobDetail pages are fetched (no IBM board
enumeration). JobDetail is behind an AWS WAF JS challenge for plain HTTP, so
details are fetched with a WAF-warmed browser session.
"""
import gzip
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html

ENTRY_URL = "https://careers.confluent.io/jobs"
SEARCH_ENTRY_URL = "https://www.ibm.com/careers/search?q=confluent"
SEARCH_API_URL = "https://www-api.ibm.com/search/api/v2"
DETAIL_HOST = "careers.ibm.com"
JOB_ID_PATTERN = re.compile(r"^JobDetail\?jobId=(\d+)$")
WIDGET_ID = "jobDetailWidget"
SEARCH_PAGE_SIZE = 30

# Exact search request body observed from the running IBM search app on the
# official entry page (aggs are reporting-only; membership comes from query).
_SEARCH_BODY = {
    "appId": "careers",
    "scopes": ["careers2"],
    "query": {"bool": {"must": [{"simple_query_string": {
        "query": "confluent",
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
    "sm": {"query": "confluent", "lang": "zz"},
    "_source": ["_id", "title", "url", "description", "language", "entitled",
                "field_keyword_17", "field_keyword_08", "field_keyword_18",
                "field_keyword_19"],
}


def compact_body_hash(html):
    """Mirror the browser-reference NFC/whitespace-free body hash."""
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def title_slug(title):
    slug = re.sub(r"\s+", "-", str(title or "").strip())
    slug = re.sub(r"[^A-Za-z0-9\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "job"


def pretty_detail_url(title, job_id):
    if not re.fullmatch(r"\d+", str(job_id)):
        raise ValueError(f"Confluent exposed jobId is not numeric: {job_id!r}")
    return f"https://{DETAIL_HOST}/en_US/careers/JobDetail/{title_slug(title)}/{job_id}"


# --- Round-3 membership rule (user scope decision 2026-11-08) -----------------
# Membership is only parent-board postings explicitly attributed to Confluent by:
# (a) a structured field (legal employer / business unit / brand / team /
#     organization) naming Confluent, or, where no such field exists,
# (b) an explicit company name in the (listing) job title, or
# (c) the role's own description header naming Confluent as the hiring
#     business. Roles that merely mention Confluent as a product/skill/
#     customer are excluded.
# The IBM parent board exposes no Confluent legal-employer/business-unit
# facet value (legal-employer facet = 12 IBM entities; the Confluent-tagged
# keyword facet is a skill tag and is NOT counted). So (a) yields 0 here.
# Description header = the Introduction section plus the opening 800
# characters of "Your role and responsibilities" (the role-specific opening
# after generic IBM boilerplate). Hiring-business patterns below name
# Confluent as employer/team, not as product, deployment, subscription,
# customer segment, or EEO boilerplate.
_HEADER_HIRING_PATTERNS = [
    r"\bat confluent\b",
    r"\bwith confluent(?:,|\s)",
    r"\bnow part of ibm,?\s+confluent brings\b",
    r"\bconfluent brings its\b",
    r"\bconfluent helps?\b",
    r"\bibm/confluent is searching\b",
    r"\bibm confluent is searching\b",
    r"\bmember of the confluent\b.{0,40}\bteam\b",
    r"\bconfluent (?:sales|customer success|business systems|solutions|support) team\b",
    r"\bconfluent, an ibm company\b",
]
_HEADER_HIRING_RE = [re.compile(p, re.I) for p in _HEADER_HIRING_PATTERNS]
_HEADER_PRODUCT_PATTERNS = [
    r"confluent deployment", r"confluent cloud", r"confluent's technology",
    r"confluent subscription", r"confluent platform", r"confluent customers",
    r"confluent is an equal opportunity", r"confluent's capabilities",
    r"confluent solutions", r"confluent offerings",
]
_HEADER_PRODUCT_RE = [re.compile(p, re.I) for p in _HEADER_PRODUCT_PATTERNS]


def description_header_text(sections):
    sections = sections or {}
    intro = sections.get("Introduction") or ""
    resp = sections.get("Your role and responsibilities") or ""
    return (intro + " " + resp[:800]).strip()


def membership_decision(*, search_title, sections=None, metadata=None, employer=None):
    """Apply the round-3 user membership rule. Returns a decision dict."""
    sections = sections or {}
    metadata = metadata or {}
    structured_values = [
        employer or metadata.get("Company") or "",
        metadata.get("Area of work") or "",
    ]
    for value in structured_values:
        if "confluent" in str(value).lower():
            return {"included": True, "rule": "structured_legal_employer_or_business_unit",
                    "evidence": str(value)[:200]}
    if "confluent" in str(search_title or "").lower():
        return {"included": True, "rule": "listing_title_contains_confluent",
                "evidence": str(search_title)[:200]}
    header = description_header_text(sections)
    for pattern, rx in zip(_HEADER_HIRING_PATTERNS, _HEADER_HIRING_RE):
        if rx.search(header):
            return {"included": True, "rule": "description_header_names_confluent_as_hiring_business",
                    "evidence": pattern}
    # Excluded: record the closest Confluent mention type for audit.
    full = " ".join(str(v) for v in sections.values())
    reason = "no_confluent_attribution_in_title_or_header"
    for rx in _HEADER_PRODUCT_RE:
        if rx.search(header) or rx.search(full):
            reason = "confluent_mentioned_only_as_product_customer_or_skill"
            break
    if "confluent" in full.lower():
        # Distinguish EEO-only / deep-body mentions still as product-ish.
        if reason.startswith("no_confluent"):
            reason = "confluent_mentioned_only_outside_title_or_header"
    return {"included": False, "rule": "excluded_keyword_hit", "evidence": reason}


def _parse_job_id(search_url):
    parts = urlsplit(str(search_url))
    if parts.hostname != DETAIL_HOST or parts.scheme != "https":
        raise ValueError(f"Confluent search exposes a non-IBM detail URL: {search_url}")
    if parts.path.rstrip("/") != "/careers/JobDetail":
        raise ValueError(f"Confluent search detail URL has no jobId path: {search_url}")
    values = parse_qs(parts.query).get("jobId", [])
    if len(values) != 1 or not re.fullmatch(r"\d+", values[0]):
        raise ValueError(f"Confluent search detail URL has no numeric jobId: {search_url}")
    return values[0]


def _parse_detail(html, job_id):
    """Extract widget sections, metadata article and JSON-LD from a JobDetail page."""
    soup = BeautifulSoup(html, "lxml")
    widget = soup.find(id=WIDGET_ID)
    if widget is None:
        raise ValueError(f"JobDetail {job_id} has no #{WIDGET_ID} element")
    sections = {}
    for field in widget.select(".article__content__view__field"):
        label_el = field.select_one(".article__content__view__field__label")
        value_el = field.select_one(".article__content__view__field__value")
        label = label_el.get_text(" ", strip=True) if label_el else ""
        if value_el is None:
            continue
        if label:
            sections[label] = text_html(value_el.decode_contents())
        else:
            # Unlabeled field (e.g. "Years of Experience: 4 - 8"): keep it
            # under its leading keyword so no section text is dropped.
            plain = text_html(value_el.decode_contents())
            if plain.startswith("Years of Experience:"):
                sections["Years of Experience"] = plain.partition(":")[2].strip().replace("\xa0", " ")
    boilerplate = [h.get_text(" ", strip=True) for h in widget.find_all("h3")]
    metadata = {}
    for article in soup.find_all("article"):
        if "article--details" not in (article.get("class") or []):
            continue
        for field in article.select(".article__content__view__field"):
            label_el = field.select_one(".article__content__view__field__label")
            value_el = field.select_one(".article__content__view__field__value")
            label = label_el.get_text(" ", strip=True) if label_el else ""
            value = value_el.get_text(" ", strip=True) if value_el is not None else ""
            if label:
                metadata[label] = value
    jsonld = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            jsonld = data
            break
    apply_url = None
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/careers/Login?jobId=" in href:
            apply_url = href
            break
    return {
        "sections": sections,
        "boilerplate_headings": boilerplate,
        "metadata": metadata,
        "jsonld": jsonld,
        "apply_url": apply_url,
        "widget_html": widget.decode_contents(),
    }


class Confluent(Adapter):
    """Follow Confluent's 301 -> IBM search -> exact JobDetail pages."""

    VERIFICATION_REVISION = "confluent.ibm-careers-search-and-jobdetail.2"
    SEARCH_API_URL = SEARCH_API_URL

    def __init__(self, http, company, source, options, detail_fetcher=None):
        super().__init__(http, company, source, options)
        # An async callable (job_id, title) -> (html_text, final_url, http_status).
        # Defaults to a WAF-warmed Playwright session; tests inject fixtures.
        self.detail_fetcher = detail_fetcher
        self._browser = None

    # -- listing ---------------------------------------------------------
    @staticmethod
    def _search_body(from_offset):
        body = json.loads(json.dumps(_SEARCH_BODY))
        if from_offset:
            body["from"] = from_offset
        return body

    @staticmethod
    def _search_hits(payload, label):
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
            raise ValueError(f"Confluent {label} search response has no hits")
        hits = payload["hits"]["hits"]
        total = payload["hits"]["total"]
        if not isinstance(hits, list) or not isinstance(total, dict):
            raise ValueError(f"Confluent {label} search response is malformed")
        if total.get("relation") != "eq" or not isinstance(total.get("value"), int):
            raise ValueError(f"Confluent {label} search total is not exact")
        ids = []
        for hit in hits:
            src = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(src, dict) or not src.get("url") or not src.get("title"):
                raise ValueError(f"Confluent {label} search hit lacks url/title")
            ids.append(_parse_job_id(src["url"]))
        if len(ids) != len(set(ids)):
            raise ValueError(f"Confluent {label} search page repeats a jobId")
        return hits, total["value"]

    # -- archival (browser details share the HTTP archive schema) ---------
    def _archive(self, *, url, final_url, status, method, content: bytes, request_body=None):
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
            "url": url, "final_url": final_url, "method": method,
            "request_body": request_body, "status": status, "sha256": digest,
            "content_type": "text/html", "redirects": [], "fetched_at": now(),
            "fetcher": "playwright-chromium",
        }
        with (output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    # -- detail fetching ---------------------------------------------------
    async def _browser_fetch(self, job_id, title):
        """Fetch one pretty JobDetail page in the WAF-warmed browser session.

        Transient non-200s (e.g. gateway 502s) are retried a bounded number
        of times; persistent failures raise and are recorded per job.
        """
        from playwright.async_api import async_playwright

        url = pretty_detail_url(title, job_id)
        if self._browser is None:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            )
            page = await ctx.new_page()
            # Warm up: one query-form navigation establishes the WAF cookies.
            # (The query-form page itself carries no description; it
            # only needs to settle the challenge.)
            await page.goto(
                f"https://{DETAIL_HOST}/careers/JobDetail?jobId={job_id}",
                wait_until="domcontentloaded", timeout=60000,
            )
            await page.wait_for_timeout(3000)
            self._browser = (pw, browser, page)
        pw, browser, page = self._browser
        last_error = None
        for attempt in range(3):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = response.status if response is not None else None
                if status != 200:
                    last_error = ValueError(f"JobDetail {job_id} returned HTTP {status}")
                    await page.wait_for_timeout(3000)
                    continue
                try:
                    await page.wait_for_function(
                        "() => { const el = document.getElementById('jobDetailWidget'); "
                        "return el && el.innerText && el.innerText.length > 500; }",
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
            except Exception as exc:
                last_error = exc
                await page.wait_for_timeout(3000)
        raise last_error or ValueError(f"JobDetail {job_id} fetch failed")

    async def _close_browser(self):
        if self._browser is not None:
            pw, browser, page = self._browser
            self._browser = None
            try:
                await browser.close()
            finally:
                await pw.stop()

    # -- job assembly ------------------------------------------------------
    def _make_job(self, hit, detail_html, detail_url, detail_status):
        src = hit["_source"]
        job_id = _parse_job_id(src["url"])
        parsed = _parse_detail(detail_html, job_id)
        meta = parsed["metadata"]
        sections = parsed["sections"]
        jsonld = parsed["jsonld"]
        # Cross-check identity: the page must describe the requested job.
        if meta.get("Job ID") != job_id or str(jsonld.get("title", "")).strip() == "":
            raise ValueError(f"JobDetail {job_id} identity fields do not match the listing")
        widget_html = parsed["widget_html"]
        description_text = text_html(widget_html)
        if len(description_text.strip()) < 100:
            raise ValueError(f"JobDetail {job_id} description is missing or implausibly short")
        date_posted = jsonld.get("datePosted") or None
        if date_posted is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_posted)):
            date_posted = str(date_posted)
        city = meta.get("City / Township / Village")
        state = meta.get("State / Province")
        country = meta.get("Country")
        raw = {
            "search": src,
            "search_hit_id": hit.get("_id"),
            "search_hit_index": hit.get("_index"),
            "search_hit_sort": hit.get("sort"),
            "search_score": hit.get("_score"),
            "search_title": src.get("title"),
            "search_location": src.get("field_keyword_19"),
            "snipped_description": src.get("description"),
            "detail_sections": sections,
            "detail_metadata": meta,
            "detail_jsonld": jsonld,
            "widget_html": widget_html,
        }
        job = self.job(
            raw,
            title=src["title"],
            url=detail_url,
            job_id=job_id,
            requisition_id=None,
            apply_url=parsed["apply_url"],
            description=widget_html,
            method="confluent.ibm_careers_jobdetail.pretty_url.widget",
            description_url=detail_url,
            locations=[{"city": city, "state": state, "country": country}],
            department=meta.get("Area of work") or src.get("field_keyword_08"),
            team=None,
            employment_type=meta.get("Employment type"),
            workplace_type=meta.get("Work arrangement"),
            date_posted=date_posted,
            date_posted_raw=date_posted,
            date_updated=None,
            salary=None,
            qualifications=sections.get("Required technical and professional expertise"),
            responsibilities=sections.get("Your role and responsibilities"),
            experience=sections.get("Years of Experience"),
            education=sections.get("Required education"),
            employer=meta.get("Company"),
        )
        job.description_text = description_text
        job.record_identity = f"confluent:{job_id}"
        return job

    def _unavailable_job(self, hit):
        src = hit["_source"]
        job_id = _parse_job_id(src["url"])
        raw = {"search": src, "search_hit_id": hit.get("_id"),
               "search_hit_index": hit.get("_index"),
               "search_hit_sort": hit.get("sort"),
               "search_score": hit.get("_score"),
               "search_title": src.get("title"),
               "search_location": src.get("field_keyword_19"),
               "snipped_description": src.get("description"),
               "detail_fetch_failed": True}
        job = self.job(
            raw, title=src["title"],
            url=pretty_detail_url(src["title"], job_id),
            job_id=job_id, requisition_id=None, apply_url=None,
            description="", method=None, description_url=None,
            locations=[{"search_location": src.get("field_keyword_19")}],
            department=src.get("field_keyword_08"),
        )
        job.description_unavailable = True
        job.record_identity = f"confluent:{job_id}"
        job.warnings.append("detail_fetch_failed")
        return job

    # -- run ---------------------------------------------------------------
    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in {"careers.confluent.io"}
                or parts.path.rstrip("/") != "/jobs"):
            raise ValueError("Confluent dedicated flow must start from https://careers.confluent.io/jobs")

        listing_started = time.perf_counter()
        entry = await self.http.get(entry_url)
        if str(entry.url) != SEARCH_ENTRY_URL:
            raise ValueError(
                f"Confluent official entry no longer redirects to the IBM search: "
                f"{entry.url} (expected {SEARCH_ENTRY_URL})")
        if ("career-search" not in entry.text or "careers2" not in entry.text
                or 'appid: \'careers\'' not in entry.text):
            raise ValueError("IBM search entry no longer exposes the inspected careers search component")

        # Paginate the unfiltered keyword search exactly as observed
        # (30 items/page, from=0/30/60/90), then reconcile: the index score
        # ranking shifts slightly between requests, so a boundary item can
        # straddle two pages and squeeze out one ID. Adaptive overlap rounds
        # (from shifted by -1, -2, ...) re-cover the boundaries until the
        # union of IDs reaches the search total. Every request is archived.
        pages = []
        expected_total = None
        by_id = {}
        ordered_ids = []

        def absorb(round_no, from_offset, payload):
            hits, total = self._search_hits(payload, f"round{round_no} from={from_offset}")
            page_ids = [_parse_job_id(h["_source"]["url"]) for h in hits]
            pages.append({"round": round_no, "from": from_offset, "hits": len(hits),
                          "ids": page_ids, "total": total, "payload": payload})
            for hit in hits:
                job_id = _parse_job_id(hit["_source"]["url"])
                if job_id not in by_id:
                    by_id[job_id] = hit
                    ordered_ids.append(job_id)
            return total

        async def fetch_page(round_no, from_offset):
            if not self.page_guard():
                self.result.errors.append("page_budget_exhausted_during_listing")
                return None
            payload = await self.http.json(
                SEARCH_API_URL, method="POST", json=self._search_body(from_offset))
            total = absorb(round_no, from_offset, payload)
            return total

        round_no = 0
        while True:
            if round_no == 0:
                offsets = [0, SEARCH_PAGE_SIZE, 2 * SEARCH_PAGE_SIZE, 3 * SEARCH_PAGE_SIZE]
            else:
                offsets = [max(0, SEARCH_PAGE_SIZE * i - round_no) for i in range(4)]
            for from_offset in offsets:
                total = await fetch_page(round_no, from_offset)
                if total is None:
                    break
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    self.result.errors.append(
                        f"search_total_changed_between_pages: {expected_total} -> {total}")
                if len(ordered_ids) >= expected_total:
                    break
            if expected_total is None or len(ordered_ids) >= expected_total:
                break
            if round_no >= 5:
                break
            round_no += 1

        listing_seconds = time.perf_counter() - listing_started
        if expected_total is None or len(ordered_ids) != expected_total:
            self.result.errors.append(
                f"search_pagination_reconciliation_failed: unique={len(ordered_ids)} "
                f"expected={expected_total}")
        round0_pages = [p for p in pages if p["round"] == 0]
        round0_unique = list(dict.fromkeys(
            job_id for p in round0_pages for job_id in p["ids"]))
        search_total = expected_total
        # expected_total is finalised after the round-3 membership filter
        # (see below); until then it is the keyword search total.
        self.result.expected_total = expected_total

        # Details (browser, WAF-warmed). Fetch, parse and add are interleaved
        # so a crash mid-run preserves finished jobs via the checkpoint hook.
        # Per-job failures are recorded; the search record is retained as an
        # unavailable job so partial runs keep usable evidence. Membership
        # (round-3 user rule) is applied after a successful parse: excluded
        # keyword hits are recorded in metadata and are not added as jobs.
        detail_seconds = 0.0
        parse_seconds = 0.0
        failed_detail_ids = []
        excluded_jobs = []
        included_jobs = []
        fetcher = self.detail_fetcher or self._browser_fetch
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
                    self.result.errors.append(
                        f"JobDetail {job_id} ({title}): {type(exc).__name__}: {exc}")
                    self.add(self._unavailable_job(hit))
                    continue
                detail_seconds += time.perf_counter() - request_started
                if status != 200:
                    failed_detail_ids.append(job_id)
                    self.result.errors.append(
                        f"JobDetail {job_id} returned HTTP {status}")
                    self.add(self._unavailable_job(hit))
                    continue
                self._archive(url=pretty_detail_url(title, job_id),
                              final_url=final_url, status=status,
                              method="GET", content=html.encode("utf-8"))
                parse_one = time.perf_counter()
                try:
                    job = self._make_job(hit, html, final_url, status)
                except Exception as exc:
                    parse_seconds += time.perf_counter() - parse_one
                    failed_detail_ids.append(job_id)
                    self.result.errors.append(
                        f"JobDetail {job_id} parse: {type(exc).__name__}: {exc}")
                    self.add(self._unavailable_job(hit))
                    continue
                parse_seconds += time.perf_counter() - parse_one
                decision = membership_decision(
                    search_title=title,
                    sections=job.raw.get("detail_sections"),
                    metadata=job.raw.get("detail_metadata"),
                    employer=getattr(job, "employer", None))
                job.raw["membership_decision"] = decision
                if not decision["included"]:
                    excluded_jobs.append({
                        "job_id": job_id, "title": title,
                        "rule": decision["rule"], "evidence": decision["evidence"],
                        "url": pretty_detail_url(title, job_id)})
                    continue
                included_jobs.append({"job_id": job_id, "rule": decision["rule"],
                                      "evidence": decision["evidence"]})
                self.add(job)
        finally:
            await self._close_browser()

        if search_total is not None:
            self.result.expected_total = search_total - len(excluded_jobs)

        self.result.listing_complete = (
            not self.result.errors and len(by_id) == search_total)

        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_browser_seconds"] = round(detail_seconds, 6)
        self.result.timings["detail_parse_seconds"] = round(parse_seconds, 6)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": SEARCH_ENTRY_URL,
            "requested_entry_url": entry_url,
            "search_api_url": SEARCH_API_URL,
            "membership_authority": (
                "Round-3 user scope decision 2026-11-08: collect only parent-board "
                "postings explicitly attributed to Confluent by a structured field "
                "(legal employer/business unit/brand/team) or, where no such field "
                "exists, an explicit name in the listing title or the description "
                "header naming Confluent as hiring business. The IBM board's "
                "legal-employer facet (field_keyword_20, probed via the same API) "
                "contains 12 IBM entities and no Confluent entity, and its UI "
                "facets are only Remote/Hybrid, Career Area, Experience, Location, "
                "so the structured-field rule yields 0. The remaining keyword hits "
                "are then filtered by listing title and by description header "
                "(Introduction + first 800 chars of responsibilities) hiring-"
                "business patterns in membership_decision(); product/customer/"
                "skill-only mentions are excluded and recorded in excluded_jobs. "
                "Confluent's own domain 301 chain to IBM's careers search "
                "(scopeid=careers2, appid=careers) with keyword `confluent` is "
                "the enumeration journey; only its exact JobDetail pages are fetched."),
            "listing_filter_behavior": (
                "unfiltered search; facet aggregations (Remote/Hybrid, Career "
                "Areas, Experience level, Location) are computed over all "
                "matches and no facet was applied"),
            "ui_pagination": (
                "30 items per page, observed `from` offsets 0/30/60/90 "
                "(1–30 of 91 items); adaptive overlap rounds (from-1, -2, up to 5) "
                "re-cover score-ranking boundary shifts until the union reaches "
                "the search total"),
            "search_total": search_total,
            "membership_rule": (
                "structured_legal_employer_or_business_unit (0; legal-employer "
                "facet has no Confluent value) OR listing_title_contains_confluent "
                "OR description_header_names_confluent_as_hiring_business "
                "(header=Introduction+resp[:800], hiring patterns in "
                "membership_decision). Product/customer/skill-only keyword hits excluded."),
            "included_jobIds": [entry["job_id"] for entry in included_jobs],
            "included_by_rule": included_jobs,
            "excluded_jobs": excluded_jobs,
            "counts_by_rule": {
                "structured_legal_employer_or_business_unit": sum(1 for e in included_jobs if e["rule"] == "structured_legal_employer_or_business_unit"),
                "listing_title_contains_confluent": sum(1 for e in included_jobs if e["rule"] == "listing_title_contains_confluent"),
                "description_header_names_confluent_as_hiring_business": sum(1 for e in included_jobs if e["rule"] == "description_header_names_confluent_as_hiring_business"),
                "excluded_keyword_hits": len(excluded_jobs),
            },
            "search_pages": [
                {"round": p["round"], "from": p["from"], "hits": p["hits"],
                 "total": p["total"], "jobIds": p["ids"]} for p in pages],
            "search_jobIds_in_order": ordered_ids,
            "round0_unique_jobIds": round0_unique,
            "reconciliation_rounds": max(p["round"] for p in pages) if pages else 0,
            "detail_failed_jobIds": failed_detail_ids,
            "detail_url_pattern": "https://careers.ibm.com/en_US/careers/JobDetail/<slug>/<jobId> (slug ignored by server; jobId is authoritative)",
            "description_source": (
                "each exposed job's pretty JobDetail page #jobDetailWidget "
                "sections (Introduction, responsibilities, education, expertise, "
                "employer boilerplate); search `description` is a ~250-char "
                "truncated snippet and was never used as the description"),
            "application_links": (
                "each page's Apply Now href (Login?jobId=) retained as apply_url; "
                "no application form or login was opened"),
            "dates": "datePosted from each page's JSON-LD JobPosting; no updated dates exposed",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "listing_requests": len(pages),
            "detail_requests": len(ordered_ids),
        })
        self.result.timings["source_snapshot_phase_seconds"] = round(
            self.result.metadata["collection_wall_seconds"], 6)
        return self.result
