"""Hyprbots Systems (hyperbots.com) official careers journey (reviewed 2026-11-07).

The ledger URL ``https://www.hyprbots.com/careers`` is a company alias domain:
it 301-redirects to ``https://www.hyperbots.com/careers`` on the official
Hyperbots site.  That official route is **not published**: the server answers
``404`` with the site's own canonical ``/404`` page (title "Hyperbots", h1
"ERROR 404", a single "Go to home page" link).

The official site itself (Framer, published 2026-11-02) publishes no careers or
jobs page anywhere: its ``sitemap.xml`` lists 258 URLs, the robots-declared
glossary sitemap lists 49,455 definition URLs, its own Framer search index lists
257 pages, and the homepage navigation has no careers link.  The only
hiring-adjacent external link is the company's LinkedIn page.  No ATS iframe,
script, link, job API or ``JobPosting`` JSON-LD exists to authorise any board,
so no guessed ATS tenant, parent board or aggregator is substituted.

This adapter therefore records the absence explicitly instead of returning a
successful zero-job listing.  It fails closed: if the ledger URL starts serving
a real page, if the official site grows a careers link/page, or if any listing
signal appears, it reports ``unreviewed_careers_journey_detected`` so the new
journey is inspected before any membership is collected.

The shared HTTP client raises on 4xx, so the 404 body is read back from the
client's own content-addressed response archive (the same pattern as the
Clumio/CASA Retail AI 404 reviews).  No extra unarchived request is made.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.hyprbots.com/careers"
OFFICIAL_SITE = "https://www.hyperbots.com/"
OFFICIAL_SITEMAP = "https://www.hyperbots.com/sitemap.xml"
GLOSSARY_SITEMAP = "https://www.hyperbots.com/glossary/sitemap.xml"
OFFICIAL_HOSTS = frozenset({"hyperbots.com", "www.hyperbots.com"})
LEDGER_HOSTS = frozenset({"hyprbots.com", "www.hyprbots.com"})
EXPECTED_404_CANONICAL_PATH = "/404"
EXPECTED_HOMEPAGE_CANONICAL = "https://www.hyperbots.com/"
EXPECTED_HOMEPAGE_TITLE = "Best AI for Finance & Accounting | Hyperbots Agentic Platform"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# A real external job board/ATS host.  None appears on the official site; the
# pattern exists so a future change is detected and reviewed, not scraped.
ATS_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io|"
    r"jobs\.lever\.co|lever\.co|"
    r"(?:[a-z0-9-]+\.)?myworkdayjobs\.com|workday\.com|"
    r"ashbyhq\.com|smartrecruiters\.com|"
    r"keka\.com|darwinbox\.com|darwinbox\.in|"
    r"zohorecruit\.com|zohorecruit\.in|"
    r"turbohire\.co|recruitee\.com|workable\.com|bamboohr\.com|freshteam\.com|"
    r"hirepro\.in|recruiterflow\.com|jazz\.co|applytojob\.com|jobvite\.com|"
    r"icims\.com|taleo\.net|oraclecloud\.com|successfactors\.com|"
    r"phenompeople\.com|eightfold\.ai"
    r")$"
)
# Aggregator hostnames are only a listing signal for their *job-search* paths.
# The official homepage links the company's LinkedIn profile (identity), which
# must not be mistaken for a job board.
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:linkedin\.com|indeed\.com|glassdoor\.com|instahyre\.com|"
    r"wellfound\.com|cutshort\.io|naukri\.com)$"
)
AGGREGATOR_JOB_PATH = re.compile(r"(?i)^/(?:jobs?|careers?|vacanc|job-search|company/[^/]+/jobs)")
# Career/job *paths* on the official site.  Segment-anchored so glossary pages
# such as /glossary/job-order-costing never count as a careers page.
CAREER_PATH = re.compile(
    r"(?i)^/(?:careers?|jobs?|roles?|open-roles|open-positions|join-us|"
    r"we-are-hiring|hiring)(?:/|$)"
)
LINKEDIN_COMPANY = re.compile(r"(?i)^https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^/?#]+")
FRAMER_SEARCH_INDEX = re.compile(r"^https://framerusercontent\.com/sites/[A-Za-z0-9]+/searchIndex-[A-Za-z0-9]+\.json$")


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def _count_job_postings(value) -> int:
    if isinstance(value, dict):
        types = value.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            return 1
        return sum(_count_job_postings(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_job_postings(item) for item in value)
    return 0


def job_posting_schema_count(soup: BeautifulSoup) -> int:
    total = 0
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            total += _count_job_postings(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return total


def page_signals(html: str, response_url: str) -> dict:
    """Describe every job-listing signal on one official-site page."""
    soup = BeautifulSoup(html, "lxml")
    anchors, listing_candidates, external_ats_links = [], [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _absolute(response_url, href)
        item = {"text": normalize_text(anchor.get_text(" ", strip=True)), "href": href, "url": absolute}
        anchors.append(item)
        host, path = _host(absolute), urlsplit(absolute).path
        if ATS_HOST.search(host) or (AGGREGATOR_HOST.search(host) and AGGREGATOR_JOB_PATH.match(path)):
            external_ats_links.append(item)
        elif CAREER_PATH.match(path):
            listing_candidates.append(item)
    ats_embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    text = visible_text(html)
    normalized = normalize_text(text)
    canonical_tag = soup.select_one('link[rel="canonical"]')
    return {
        "response_url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h1": [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")],
        "headings": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "canonical": canonical_tag.get("href") if canonical_tag else None,
        "anchors": anchors,
        "anchor_count": len(anchors),
        "listing_candidates": listing_candidates,
        "external_ats_links": external_ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "visible_text_chars": len(text),
        "normalized_text_chars": len(normalized),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["listing_candidates"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
    )


def parse_entry_response(*, status_code: int, final_url: str, html: str,
                         history: list[dict] | None = None) -> dict:
    """Validate the ledger-URL journey and classify what the official site serves.

    ``history`` items are ``{"status", "url", "location"}`` hops observed before
    ``final_url``; ``status`` may be ``None`` when the shared HTTP archive kept
    only hop URLs.  A ledger host must redirect to the official host; anything
    else is a changed journey and raises ``ValueError`` for human review.
    """
    hops = list(history or [])
    if not hops:
        raise ValueError("ledger careers URL no longer redirects; the reviewed journey changed")
    first = urlsplit(hops[0].get("url") or "")
    if (first.hostname or "").lower() not in LEDGER_HOSTS:
        raise ValueError(f"ledger careers URL journey no longer starts on hyprbots.com: {hops[0]}")
    for hop in hops:
        if hop.get("status") is not None and hop["status"] not in REDIRECT_STATUSES:
            raise ValueError(f"unexpected redirect status {hop['status']} in {hop.get('url')}")
    parts = urlsplit(final_url)
    if (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"careers entry redirected to an unexpected host: {final_url}")
    if parts.path.rstrip("/") != "/careers":
        raise ValueError(f"careers entry redirected to an unexpected path: {final_url}")

    parsed = page_signals(html, final_url)
    parsed["status_code"] = status_code
    parsed["redirects"] = [
        {"url": hop.get("url"), "status": hop.get("status"), "location": hop.get("location")}
        for hop in hops
    ]
    parsed["redirect_hop_statuses_known"] = all(hop.get("status") is not None for hop in hops)
    parsed["html_chars"] = len(html)
    parsed["html_text_sha256"] = sha256(html.encode("utf-8")).hexdigest()
    if status_code == 404:
        canonical = parsed["canonical"] or ""
        canonical_path = urlsplit(canonical).path.rstrip("/")
        if canonical_path != EXPECTED_404_CANONICAL_PATH:
            raise ValueError(f"official 404 page canonical changed: {canonical!r}")
        parsed["kind"] = "official_404"
    elif status_code == 200:
        parsed["kind"] = "published_page"
    else:
        raise ValueError(f"official careers URL returned unreviewed status {status_code}")
    return parsed


def parse_homepage(html: str, response_url: str = OFFICIAL_SITE) -> dict:
    """Parse the official Hyperbots homepage and its published page index URL."""
    parts = urlsplit(response_url)
    if (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"official homepage redirected to an unexpected host: {response_url}")
    if parts.path not in ("", "/"):
        raise ValueError(f"official homepage response came from an unexpected path: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    canonical_tag = soup.select_one('link[rel="canonical"]')
    canonical = canonical_tag.get("href") if canonical_tag else None
    if canonical != EXPECTED_HOMEPAGE_CANONICAL:
        raise ValueError(f"official homepage canonical changed: {canonical!r}")
    parsed = page_signals(html, response_url)
    meta = soup.select_one('meta[name="framer-search-index"]')
    search_index = meta.get("content") if meta else None
    if not search_index or not FRAMER_SEARCH_INDEX.match(search_index):
        raise ValueError(f"official site search index reference changed: {search_index!r}")
    parsed.update({
        "search_index_url": search_index,
        "title_matches": parsed["title"] == EXPECTED_HOMEPAGE_TITLE,
        "linkedin_company_urls": sorted({
            item["url"] for item in parsed["anchors"]
            if LINKEDIN_COMPANY.match(item["url"])
        }),
    })
    return parsed


def parse_sitemap(xml: str) -> dict:
    """List the official sitemap URLs and flag any career/job page."""
    urls = [value.strip() for value in re.findall(r"<loc>(.*?)</loc>", xml)]
    if not urls:
        raise ValueError("official sitemap contains no <loc> URLs")
    hosts = sorted({(urlsplit(url).hostname or "").lower() for url in urls})
    foreign = [url for url in urls if (urlsplit(url).hostname or "").lower() not in OFFICIAL_HOSTS]
    career_like = [url for url in urls if CAREER_PATH.match(urlsplit(url).path)]
    return {
        "url_count": len(urls),
        "hosts": hosts,
        "foreign_host_urls": foreign,
        "career_like_urls": career_like,
        "urls": urls,
    }


def parse_site_search_index(payload) -> dict:
    """List the pages in the site's own Framer search index."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("official site search index is not a non-empty path map")
    pages = sorted(payload)
    foreign = [page for page in pages
               if urlsplit(page).scheme or not page.startswith("/")]
    career_like = [page for page in pages if CAREER_PATH.match(urlsplit(page).path)]
    return {
        "page_count": len(pages),
        "foreign_paths": foreign,
        "career_like_pages": career_like,
        "pages": pages,
    }


def _entry_evidence(parsed: dict) -> dict:
    return {
        "kind": parsed["kind"],
        "final_url": parsed["response_url"],
        "status_code": parsed["status_code"],
        "canonical": parsed["canonical"],
        "title": parsed["title"],
        "h1": parsed["h1"],
        "html_text_sha256": parsed["html_text_sha256"],
        "redirects": parsed["redirects"],
    }


def _redirect_evidence(response) -> list[dict]:
    return [
        {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
        for item in response.history
    ]


class HyprbotsSystems(Adapter):
    """Read the official Hyperbots pages and record that no careers page exists."""

    def _last_capture(self, url: str, since_line: int) -> dict:
        if not self.http.output:
            return {}
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return {}
        lines = path.read_text(encoding="utf-8").splitlines()
        for raw in reversed(lines[since_line:]):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (record.get("url") == url or record.get("final_url") == url
                    or url in record.get("redirects", [])):
                return record
        return {}

    def _captured_body(self, record: dict) -> bytes | None:
        digest = record.get("sha256")
        if not digest or not self.http.output:
            return None
        path = self.http.output / "responses" / f"{digest}.gz"
        if not path.exists():
            return None
        return gzip.decompress(path.read_bytes())

    def _request_log_lines(self) -> int:
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    async def _fetch(self, url: str, purpose: str):
        """GET through the shared client; on 4xx read the archived body back."""
        if not self.page_guard():
            return None, {"url": url, "purpose": purpose, "outcome": "page_budget_exhausted"}
        since = self._request_log_lines()
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            capture = self._last_capture(url, since)
            raw = self._captured_body(capture)
            record = {
                "url": url,
                "purpose": purpose,
                "outcome": "request_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "final_url": capture.get("final_url"),
                "status_code": capture.get("status"),
                "html_bytes": len(raw) if raw is not None else None,
                "html_sha256": capture.get("sha256"),
                "redirects": [
                    {"url": item, "status": None, "location": None}
                    for item in capture.get("redirects", [])
                ],
                "redirect_hop_statuses_known": False,
                "body": raw,
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
            return None, record
        raw = response.content
        return response, {
            "url": url,
            "purpose": purpose,
            "outcome": "received",
            "final_url": str(response.url),
            "status_code": response.status_code,
            "html_bytes": len(raw),
            "html_sha256": sha256(raw).hexdigest(),
            "redirects": _redirect_evidence(response),
            "redirect_hop_statuses_known": True,
            "body": raw,
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }

    @staticmethod
    def _public_record(record: dict) -> dict:
        return {key: value for key, value in record.items() if key != "body"}

    @staticmethod
    def _evidence_from_record(record: dict) -> dict:
        return {
            "requested_url": record.get("url"),
            "final_url": record.get("final_url"),
            "status_code": record.get("status_code"),
            "html_bytes": record.get("html_bytes"),
            "html_sha256": record.get("html_sha256"),
            "redirects": record.get("redirects"),
            "error": record.get("error"),
            "fetched_at": now(),
        }

    async def run(self):
        source_started = time.perf_counter()

        def finish(seconds_key: str):
            elapsed = max(0.0, time.perf_counter() - source_started)
            self.result.metadata["source_collection_wall_seconds"] = elapsed
            self.result.timings[seconds_key] = elapsed
            self.result.expected_total = None
            self.result.listing_complete = False
            if "unreviewed_careers_journey_detected" in self.result.errors:
                outcome = "unreviewed_careers_journey_detected"
            elif "no_public_careers_page" in self.result.errors:
                outcome = "failed_waitlist_no_public_careers_page"
            else:
                outcome = "failed_waitlist_journey_unavailable"
            self.result.metadata["collection_outcome"] = outcome
            return self.result

        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "expected_careers_url": "https://www.hyperbots.com/careers",
            "official_site_url": OFFICIAL_SITE,
            "official_sitemap_url": OFFICIAL_SITEMAP,
            "glossary_sitemap_url": GLOSSARY_SITEMAP,
            "listing_authority": "none: the official site publishes no careers page or job listing",
            "detail_method": "none: no role, requisition or detail route is published",
            "request_scope": (
                "Only the ledger URL, the official hyperbots.com homepage, its two robots-declared "
                "sitemaps and its own Framer search index; no guessed ATS tenant, parent-company "
                "board, aggregator or locale variant is queried."
            ),
            "verification_revision": "hyprbots-systems.official-404.1",
        })

        # 1. The ledger URL and the company's own redirect to the official site.
        _, entry_record = await self._fetch(OFFICIAL_ENTRY, "official_careers_entry")
        try:
            body = entry_record.get("body")
            if body is None:
                raise ValueError("official careers entry response body is unavailable")
            entry = parse_entry_response(
                status_code=entry_record["status_code"],
                final_url=entry_record.get("final_url") or "",
                html=body.decode("utf-8", errors="replace"),
                history=entry_record.get("redirects") or [],
            )
        except Exception as exc:
            self.result.metadata["initial_entry"] = self._evidence_from_record(entry_record)
            self.result.errors.append(f"hyprbots_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["observed_journey"] = {
                "conclusion": "The ledger careers URL could not be validated against the reviewed journey."
            }
            return finish("hyprbots_source_collection_seconds")

        self.result.metadata["initial_entry"] = {
            **self._evidence_from_record(entry_record),
            "canonical": entry["canonical"],
            "title": entry["title"],
            "h1": entry["h1"],
            "redirect_hop_statuses_known": entry["redirect_hop_statuses_known"],
            "redirect_status_source": (
                "Exact 301 hop is captured in source/careers-entry-20261107.json and "
                "browser-reference-worker.json; the shared HTTP archive keeps hop URLs only."
            ),
        }
        self.result.metadata["observed_journey"] = {
            "ledger_redirect_chain": entry["redirects"],
            "final_url": entry["response_url"],
            "status_code": entry["status_code"],
            "canonical": entry["canonical"],
            "title": entry["title"],
            "h1": entry["h1"],
            "headings": entry["headings"],
            "anchor_count": entry["anchor_count"],
            "anchors": entry["anchors"],
            "job_listing_candidates": entry["listing_candidates"],
            "external_ats_links": entry["external_ats_links"],
            "ats_embeds": entry["ats_embeds"],
            "job_posting_schema_count": entry["job_posting_schema_count"],
            "visible_text_chars": entry["visible_text_chars"],
            "normalized_text_chars": entry["normalized_text_chars"],
            "normalized_text_sha256": entry["normalized_text_sha256"],
        }

        if entry["kind"] != "official_404":
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The ledger careers URL now serves a published page instead of the reviewed 404; "
                "the new journey must be inspected before any membership is collected."
            )
            return finish("hyprbots_source_collection_seconds")
        if has_listing_signal(entry):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The 404 response now carries a job-listing signal; the journey must be inspected "
                "before any membership is collected."
            )
            return finish("hyprbots_source_collection_seconds")

        # 2. The official homepage: navigation, identity and page index reference.
        _, home_record = await self._fetch(OFFICIAL_SITE, "official_homepage")
        try:
            if home_record.get("body") is None:
                raise ValueError("official homepage response body is unavailable")
            home = parse_homepage(home_record["body"].decode("utf-8", errors="replace"),
                                  home_record.get("final_url") or "")
        except Exception as exc:
            self.result.metadata["official_site"] = self._evidence_from_record(home_record)
            self.result.errors.append(f"hyprbots_official_site_failed: {type(exc).__name__}: {exc}")
            return finish("hyprbots_source_collection_seconds")

        self.result.metadata["official_site"] = {
            **self._evidence_from_record(home_record),
            "canonical": home["canonical"],
            "title": home["title"],
            "title_matches_reviewed": home["title_matches"],
            "h1": home["h1"],
            "anchor_count": home["anchor_count"],
            "internal_paths": sorted({
                urlsplit(item["url"]).path for item in home["anchors"]
                if (urlsplit(item["url"]).hostname or "").lower() in OFFICIAL_HOSTS
            }),
            "linkedin_company_urls": home["linkedin_company_urls"],
            "job_listing_candidates": home["listing_candidates"],
            "external_ats_links": home["external_ats_links"],
            "ats_embeds": home["ats_embeds"],
            "job_posting_schema_count": home["job_posting_schema_count"],
            "search_index_url": home["search_index_url"],
            "normalized_text_chars": home["normalized_text_chars"],
            "normalized_text_sha256": home["normalized_text_sha256"],
        }
        if has_listing_signal(home):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official homepage now links or embeds a careers/job listing; the new journey "
                "must be inspected before any membership is collected."
            )
            return finish("hyprbots_source_collection_seconds")

        # 3. The complete published page indexes: both robots-declared sitemaps
        #    and the site's own Framer search index.
        _, sitemap_record = await self._fetch(OFFICIAL_SITEMAP, "official_sitemap")
        try:
            if sitemap_record.get("body") is None:
                raise ValueError("official sitemap response body is unavailable")
            sitemap = parse_sitemap(sitemap_record["body"].decode("utf-8", errors="replace"))
        except Exception as exc:
            self.result.metadata["official_sitemap"] = self._evidence_from_record(sitemap_record)
            self.result.errors.append(f"hyprbots_official_sitemap_failed: {type(exc).__name__}: {exc}")
            return finish("hyprbots_source_collection_seconds")

        self.result.metadata["official_sitemap"] = {
            **self._evidence_from_record(sitemap_record),
            "url_count": sitemap["url_count"],
            "hosts": sitemap["hosts"],
            "foreign_host_urls": sitemap["foreign_host_urls"],
            "career_like_urls": sitemap["career_like_urls"],
        }

        _, glossary_record = await self._fetch(GLOSSARY_SITEMAP, "glossary_sitemap")
        try:
            if glossary_record.get("body") is None:
                raise ValueError("glossary sitemap response body is unavailable")
            glossary = parse_sitemap(glossary_record["body"].decode("utf-8", errors="replace"))
        except Exception as exc:
            self.result.metadata["glossary_sitemap"] = self._evidence_from_record(glossary_record)
            self.result.errors.append(f"hyprbots_glossary_sitemap_failed: {type(exc).__name__}: {exc}")
            return finish("hyprbots_source_collection_seconds")

        self.result.metadata["glossary_sitemap"] = {
            **self._evidence_from_record(glossary_record),
            "url_count": glossary["url_count"],
            "hosts": glossary["hosts"],
            "foreign_host_urls": glossary["foreign_host_urls"],
            "career_like_urls": glossary["career_like_urls"],
        }

        _, index_record = await self._fetch(home["search_index_url"], "site_search_index")
        try:
            if index_record.get("body") is None:
                raise ValueError("site search index response body is unavailable")
            site_index = parse_site_search_index(json.loads(index_record["body"]))
        except Exception as exc:
            self.result.metadata["site_search_index"] = self._evidence_from_record(index_record)
            self.result.errors.append(f"hyprbots_site_search_index_failed: {type(exc).__name__}: {exc}")
            return finish("hyprbots_source_collection_seconds")

        self.result.metadata["site_search_index"] = {
            **self._evidence_from_record(index_record),
            "page_count": site_index["page_count"],
            "foreign_paths": site_index["foreign_paths"],
            "career_like_pages": site_index["career_like_pages"],
        }

        if (sitemap["career_like_urls"] or glossary["career_like_urls"]
                or site_index["career_like_pages"] or sitemap["foreign_host_urls"]
                or glossary["foreign_host_urls"]):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official sitemap or site search index now publishes a careers/job page; the new "
                "journey must be inspected before any membership is collected."
            )
            return finish("hyprbots_source_collection_seconds")

        # 4. Exact final recheck of the same ledger URL.
        recheck_started = time.perf_counter()
        recheck = None
        try:
            _, recheck_record = await self._fetch(OFFICIAL_ENTRY, "official_entry_final_recheck")
            body = recheck_record.get("body")
            if body is None:
                raise ValueError("final recheck response body is unavailable")
            recheck = parse_entry_response(
                status_code=recheck_record["status_code"],
                final_url=recheck_record.get("final_url") or "",
                html=body.decode("utf-8", errors="replace"),
                history=recheck_record.get("redirects") or [],
            )
            self.result.metadata["final_recheck"] = {
                **self._evidence_from_record(recheck_record),
                "canonical": recheck["canonical"],
                "title": recheck["title"],
                "h1": recheck["h1"],
            }
        except Exception as exc:
            self.result.errors.append(f"hyprbots_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["hyprbots_final_recheck_seconds"] = max(
                0.0, time.perf_counter() - recheck_started)

        matches = recheck is not None and _entry_evidence(entry) == _entry_evidence(recheck)
        if recheck is not None and not matches:
            self.result.errors.append("hyprbots_ledger_entry_changed_between_requests")
        if recheck is not None and (recheck["kind"] != "official_404" or has_listing_signal(recheck)):
            self.result.errors.append("unreviewed_careers_journey_detected")

        if matches and not self.result.errors:
            self.result.errors.append("no_public_careers_page")

        self.result.metadata["membership"] = {
            "advertised_jobs": None,
            "listing_rows": 0,
            "careers_page_http_status": entry["status_code"],
            "careers_page_canonical": entry["canonical"],
            "sitemap_urls": sitemap["url_count"],
            "glossary_sitemap_urls": glossary["url_count"],
            "site_search_index_pages": site_index["page_count"],
            "career_pages_published": 0,
            "job_listing_candidates": [],
            "external_ats_links": [],
            "ats_embeds": [],
            "job_posting_schema_count": 0,
            "reconciled": True,
            "note": (
                "Membership is empty because the official site publishes no careers page or job "
                "listing on the company's own journey, not because an enumerated listing was empty."
            ),
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The ledger URL 301-redirects from the company alias domain hyprbots.com to the official "
            "site, whose /careers route is the site's own 404 page. The official homepage navigation, "
            f"the {sitemap['url_count']}-URL sitemap, the {glossary['url_count']}-URL glossary sitemap and "
            f"the {site_index['page_count']}-page Framer search index publish no careers or jobs page, "
            "and no ATS link/embed/API or JobPosting exists. The empty result is an explicit "
            "no-careers-page outcome, not a successful zero-job listing."
        )
        return finish("hyprbots_source_collection_seconds")
