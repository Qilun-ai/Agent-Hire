"""Voxela, Inc.'s official careers journey (reviewed 2026-11-08).

The ledger entry ``https://www.voxela.ai/careers`` is **not published** on
Voxela's own WordPress site: the server answers ``404`` with the site's own
"ページが見つかりませんでした – Voxela, Inc." page (h1 "404 Error",
robots noindex, body "The page you are looking for could not be found.").
The official homepage (EN and JA), Message/company-profile pages, Contact,
News, Blog and VCare pages, the robots-declared All-in-One-SEO sitemaps and
every published WordPress content index (pages, news, blog, blog_ja, posts)
publish **no** careers or jobs route and link/embed no ATS.  The only outbound
host on those pages is linkedin.com, and its six anchors are the three founder
*profile* links (identity, not a job board).  No DNS record exists for
careers./recruit./jobs./apply./hire.voxela.ai, so there is no separate
first-party careers host either.

Search engines surface only aggregators for this name (Instahyre, Wellfound,
LinkedIn, Jobaaj, bigitjobs) plus Japanese name collisions (ヴォクセル株式会社
studioVoxel, Voxel Group, Voxela Space Factory).  Per the project contract an
aggregator, a guessed ATS tenant or a search snippet is not company-published
membership, so none is collected.

This adapter therefore records the absence explicitly instead of returning a
successful zero-job listing.  It fails closed: if the ledger URL starts serving
a real page, or the official site grows a careers link/page/ATS embed/JobPosting
schema, it reports ``unreviewed_careers_journey_detected`` so that the new
journey is inspected before any membership is collected.

The shared HTTP client raises on 4xx, so the 404 body is read back from the
client's own content-addressed response archive (the same pattern used by the
other reviewed 404 companies).  No unarchived request is made.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
import json
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.voxela.ai/careers"
OFFICIAL_ENTRY_SLASH = "https://www.voxela.ai/careers/"
OFFICIAL_SITE = "https://www.voxela.ai/"
HOME_JA = "https://www.voxela.ai/ja/"
MESSAGE = "https://www.voxela.ai/message/"
MESSAGE_JA = "https://www.voxela.ai/ja/message/"
CONTACT = "https://www.voxela.ai/contact/"
NEWS = "https://www.voxela.ai/news/"
BLOG = "https://www.voxela.ai/blog/"
VCARE = "https://www.voxela.ai/vcare/"
ROBOTS = "https://www.voxela.ai/robots.txt"

SITEMAP_INDEX = "https://www.voxela.ai/sitemap.xml"
PAGE_SITEMAP = "https://www.voxela.ai/page-sitemap.xml"
NEWS_SITEMAP = "https://www.voxela.ai/news-sitemap.xml"
BLOG_SITEMAP = "https://www.voxela.ai/blog-sitemap.xml"
BLOG_JA_SITEMAP = "https://www.voxela.ai/blog_ja-sitemap.xml"
POST_ARCHIVE_SITEMAP = "https://www.voxela.ai/post-archive-sitemap.xml"

WP_TYPES = "https://www.voxela.ai/wp-json/wp/v2/types"
WP_PAGES = "https://www.voxela.ai/wp-json/wp/v2/pages?per_page=100&_fields=id,slug,link,title,status,modified"
WP_POSTS = "https://www.voxela.ai/wp-json/wp/v2/posts?per_page=100&_fields=id,slug,link,title,status,modified"
WP_BLOG = "https://www.voxela.ai/wp-json/wp/v2/blog?per_page=100&_fields=id,slug,link,title,status,modified"
WP_BLOG_JA = "https://www.voxela.ai/wp-json/wp/v2/blog_ja?per_page=100&_fields=id,slug,link,title,status,modified"
WP_NEWS = "https://www.voxela.ai/wp-json/wp/v2/news?per_page=100&_fields=id,slug,link,title,status,modified"
WP_SEARCH = "https://www.voxela.ai/wp-json/wp/v2/search"

# Bounded recheck of the obvious first-party careers routes.  These are the
# company's OWN host, not ATS guesses; every one of them answered 404 in the
# reviewed pass.  They exist so a future published route is detected.
CAREER_PATH_RECHECKS = (
    "/careers",
    "/career",
    "/jobs",
    "/recruit",
    "/join-us",
    "/ja/careers",
    "/ja/recruit",
    "/%E6%8E%A1%E7%94%A8",
)

OFFICIAL_HOSTS = frozenset({"voxela.ai", "www.voxela.ai"})
EMPLOYER_NAME = "Voxela, Inc."
EXPECTED_404_TITLE = "ページが見つかりませんでした – Voxela, Inc."
EXPECTED_404_TITLE_TOKENS = ("ページが見つかりませんでした", "Voxela, Inc.")
EXPECTED_404_H1 = "404 Error"
EXPECTED_404_BODY = "The page you are looking for could not be found."
EXPECTED_HOME_TITLE = "Voxela - AI-Powered Care Monitoring System"
EXPECTED_HOME_JA_TITLE = "Voxela（ヴォクセラ）| AI搭載24時間見守りで介護施設の安全を実現"
EXPECTED_MESSAGE_TITLE = "Our Vision for the Future of Elderly Care | Voxela"
EXPECTED_SITEMAPS = 5

# A real external job board/ATS host.  None is linked by Voxela; the pattern
# exists so a future change is detected and reviewed, never scraped by guess.
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
    r"phenompeople\.com|eightfold\.ai|herp\.cloud|hrmos\.co|"
    r"wantedly\.com"
    r")$"
)
# Aggregators are recorded as observations only; never membership.  A profile
# or company page is identity, so only job-search paths count as a board signal.
AGGREGATOR_JOB_PATH = re.compile(
    r"(?i)^/(?:jobs?|careers?|vacanc|job-search|company/[^/]+/jobs)"
)
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:linkedin\.com|indeed\.com|glassdoor\.com|instahyre\.com|"
    r"wellfound\.com|angel\.co|cutshort\.io|naukri\.com|jobaaj\.com|"
    r"bigitjobs\.com|ambitionbox\.com|doda\.jp|rikunabi\.com|mynavi\.jp|"
    r"r-agent\.com|openwork\.jp|levtech\.jp)$"
)
# Career/job *paths* on the official site.  Segment-anchored after an optional
# language prefix so a blog slug such as /blog/hiring-caregivers/ never counts.
CAREER_SEGMENT = re.compile(
    r"(?i)^/(?:careers?|jobs?|roles?|open-roles|open-positions|join-us|joinus|"
    r"we-are-hiring|hiring|employment|recruit|recruitments?|"
    r"採用|求人|採用情報|エントリー)(?:/|$)"
)
LANGUAGE_PREFIX = re.compile(r"(?i)^/(?:ja|en)(?=/|$)")
CAREER_LABEL = re.compile(
    r"(?i)(?:careers?|job openings?|open positions?|open roles?|we are hiring|"
    r"join us|採用情報|求人|募集中|エントリー)"
)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def page_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def text_fingerprint(value: str) -> dict:
    digest = sha256(value.encode("utf-8")).hexdigest()
    return {"chars": len(value), "sha256": digest}


def official_path(value: str, base: str = OFFICIAL_SITE) -> str | None:
    """Return the site path when value resolves onto the official host."""
    parts = urlsplit(urljoin(base, value or ""))
    if parts.scheme not in ("http", "https"):
        return None
    if parts.netloc.lower() not in OFFICIAL_HOSTS:
        return None
    return parts.path or "/"


def is_career_path(path: str | None) -> bool:
    if not path:
        return False
    trimmed = LANGUAGE_PREFIX.sub("", unquote(path)) or "/"
    return bool(CAREER_SEGMENT.match(trimmed))


def host_kind(host: str) -> str:
    host = (host or "").lower()
    if ATS_HOST.search(host):
        return "ats"
    if AGGREGATOR_HOST.search(host):
        return "aggregator"
    return "other"


def _jobposting_nodes(value):
    if isinstance(value, dict):
        if value.get("@type") in ("JobPosting", ["JobPosting"]):
            yield value
        for item in value.values():
            yield from _jobposting_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _jobposting_nodes(item)


def count_jobposting_schema(html: str) -> int:
    soup = BeautifulSoup(html or "", "lxml")
    total = 0
    for node in soup.find_all("script", type="application/ld+json"):
        raw = node.string or node.get_text("", strip=False) or ""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        total += sum(1 for _ in _jobposting_nodes(payload))
    return total


def scan_links(html: str, url: str) -> dict:
    """Anchors, iframes and embeds classified against the official host."""
    soup = BeautifulSoup(html or "", "lxml")
    anchors, internal_paths, external_hosts = [], [], {}
    career_links, ats_links, ats_embeds, aggregator_links = [], [], [], []
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        text = normalize_text(anchor.get_text(" ", strip=True))[:120]
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(url, href)
        parts = urlsplit(absolute)
        host = parts.netloc.lower()
        record = {"text": text, "href": href, "absolute": absolute}
        anchors.append(record)
        if host in OFFICIAL_HOSTS or not host:
            path = parts.path or "/"
            internal_paths.append(path)
            if is_career_path(path) or (CAREER_LABEL.search(text) and is_career_path(path)):
                career_links.append(record)
        else:
            external_hosts[host] = external_hosts.get(host, 0) + 1
            kind = host_kind(host)
            if kind == "ats":
                ats_links.append(record)
            elif kind == "aggregator" and AGGREGATOR_JOB_PATH.match(parts.path or "/"):
                aggregator_links.append(record)
    for frame in soup.find_all(["iframe", "embed"], src=True):
        src = urljoin(url, (frame.get("src") or "").strip())
        host = urlsplit(src).netloc.lower()
        if host and host not in OFFICIAL_HOSTS and host_kind(host) == "ats":
            ats_embeds.append({"tag": frame.name, "src": src})
    return {
        "anchor_count": len(anchors),
        "anchors": anchors,
        "internal_paths": internal_paths,
        "external_hosts": external_hosts,
        "career_links": career_links,
        "ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "aggregator_links": aggregator_links,
    }


def parse_entry_response(*, status_code: int | None, final_url: str, html: str) -> dict:
    """Classify the reviewed ledger URL response.

    ``official_404`` is the reviewed state.  ``published_page`` means the route
    now serves a real page and the journey must be inspected again.
    ``unavailable`` means the body/status could not be validated at all.
    """
    parts = urlsplit(final_url or "")
    if parts.netloc.lower() not in OFFICIAL_HOSTS:
        raise ValueError("Voxela careers entry no longer resolves to the official host")
    if (parts.path or "/").rstrip("/") != "/careers":
        raise ValueError("Voxela careers entry path changed away from /careers")
    if status_code is None:
        return {"kind": "unavailable", "response_url": final_url, "status_code": None}
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = [normalize_text(h.get_text(" ", strip=True)) for h in soup.select("h1")]
    robots = soup.select_one('meta[name="robots"]')
    robots_meta = normalize_text(robots.get("content") if robots else "")
    body = page_text(html or "")
    evidence = {
        "response_url": final_url,
        "status_code": status_code,
        "title": title,
        "h1": h1,
        "robots_meta": robots_meta,
        "canonical": (soup.select_one('link[rel="canonical"]') or {}).get("href")
        if soup.select_one('link[rel="canonical"]') else None,
        "html_lang": (soup.html.get("lang") if soup.html else None),
        "body": text_fingerprint(body),
        "jobposting_schema_count": count_jobposting_schema(html or ""),
        **scan_links(html or "", final_url),
    }
    if status_code == 404:
        missing = [token for token in EXPECTED_404_TITLE_TOKENS if token not in title]
        if missing:
            raise ValueError(f"Voxela 404 title markers changed; missing {missing}")
        if EXPECTED_404_H1 not in h1:
            raise ValueError("Voxela 404 page no longer carries the reviewed h1 '404 Error'")
        if EXPECTED_404_BODY not in body:
            raise ValueError("Voxela 404 page body no longer carries the reviewed notice")
        if "noindex" not in robots_meta.lower():
            raise ValueError("Voxela 404 page is no longer marked noindex")
        evidence["kind"] = "official_404"
        return evidence
    if status_code == 200:
        evidence["kind"] = "published_page"
        return evidence
    evidence["kind"] = "unavailable"
    return evidence


def parse_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    body = page_text(html or "")
    links = scan_links(html or "", url)
    canonical = soup.select_one('link[rel="canonical"]')
    robots = soup.select_one('meta[name="robots"]')
    return {
        "url": url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h1": [normalize_text(h.get_text(" ", strip=True)) for h in soup.select("h1")],
        "canonical": canonical.get("href") if canonical else None,
        "html_lang": (soup.html.get("lang") if soup.html else None),
        "robots_meta": normalize_text(robots.get("content") if robots else ""),
        "body": text_fingerprint(body),
        "body_text": body,
        "jobposting_schema_count": count_jobposting_schema(html or ""),
        **links,
    }


def parse_sitemap(xml_text: str) -> dict:
    text = xml_text or ""
    locs = [
        normalize_text(match)
        for match in re.findall(
            r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", text, re.S
        )
    ]
    kind = "index" if "<sitemapindex" in text else "urlset" if "<urlset" in text else None
    if kind is None:
        raise ValueError("Voxela sitemap is neither a sitemap index nor a urlset")
    hosts, foreign, career_like = {}, [], []
    for loc in locs:
        parts = urlsplit(loc)
        host = parts.netloc.lower()
        hosts[host] = hosts.get(host, 0) + 1
        if host not in OFFICIAL_HOSTS:
            foreign.append(loc)
        if is_career_path(parts.path):
            career_like.append(loc)
    return {
        "kind": kind,
        "url_count": len(locs),
        "urls": locs,
        "hosts": hosts,
        "foreign_host_urls": foreign,
        "career_like_urls": career_like,
    }


def parse_wp_index(payload, kind: str) -> dict:
    if not isinstance(payload, list):
        raise ValueError(f"Voxela WordPress {kind} index is not a list")
    items, career_like, foreign = [], [], []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError(f"Voxela WordPress {kind} row is not an object")
        link = row.get("link")
        title = row.get("title")
        if isinstance(title, dict):
            title = title.get("rendered")
        if not isinstance(link, str) or not isinstance(title, str):
            raise ValueError(f"Voxela WordPress {kind} row is missing link/title")
        item = {
            "id": row.get("id"),
            "slug": row.get("slug"),
            "link": link,
            "title": normalize_text(BeautifulSoup(title, "lxml").get_text(" ", strip=True)),
            "status": row.get("status"),
            "modified": row.get("modified"),
        }
        parts = urlsplit(item["link"])
        if parts.netloc.lower() not in OFFICIAL_HOSTS:
            foreign.append(item)
            continue
        items.append(item)
        if is_career_path(parts.path):
            career_like.append(item)
    return {
        "kind": kind,
        "count": len(items),
        "items": items,
        "links": [item["link"] for item in items],
        "career_like_items": career_like,
        "foreign_host_links": foreign,
        "ids_unique": len({item["id"] for item in items}) == len(items),
    }


def parse_wp_types(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Voxela WordPress types payload is not an object")
    bases = []
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("rest_base"), str):
            bases.append(value["rest_base"])
    job_like = [base for base in bases if re.search(r"(?i)job|career|opening|position|vacanc", base)]
    return {"rest_bases": sorted(set(bases)), "job_like_rest_bases": job_like}


def parse_wp_search(payload) -> dict:
    if not isinstance(payload, list):
        raise ValueError("Voxela WordPress search payload is not a list")
    rows = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        rows.append({
            "id": row.get("id"),
            "title": normalize_text(BeautifulSoup(row.get("title") or "", "lxml").get_text(" ", strip=True)),
            "url": row.get("url"),
            "type": row.get("type"),
            "subtype": row.get("subtype"),
        })
    return {"count": len(rows), "results": rows}


def parse_robots(text: str) -> dict:
    sitemaps, career_tokens = [], []
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "sitemap" and value:
            sitemaps.append(value)
        if CAREER_LABEL.search(value):
            career_tokens.append(line.strip())
    return {
        "sitemaps": sitemaps,
        "career_tokens": career_tokens,
        "declares_official_sitemap": SITEMAP_INDEX in sitemaps,
    }


def has_listing_signal(evidence: dict) -> bool:
    return bool(
        evidence.get("career_links")
        or evidence.get("ats_links")
        or evidence.get("ats_embeds")
        or (evidence.get("jobposting_schema_count") or 0) > 0
    )


def audit_journey(*, pages: dict, sitemaps: dict, wp_indexes: dict, wp_types: dict,
                  probes: list[dict]) -> dict:
    """Reconcile the published inventory and decide whether a listing exists."""
    career_urls = set()
    for parsed in sitemaps.values():
        career_urls.update(parsed["career_like_urls"])
    career_items = []
    for parsed in wp_indexes.values():
        career_items.extend(parsed["career_like_items"])
        career_urls.update(item["link"] for item in parsed["career_like_items"])
    career_anchors = []
    ats_links, ats_embeds, aggregator_links = [], [], []
    jobpostings = 0
    for name, parsed in pages.items():
        career_anchors.extend({"page": name, **link} for link in parsed["career_links"])
        ats_links.extend({"page": name, **link} for link in parsed["ats_links"])
        ats_embeds.extend({"page": name, **embed} for embed in parsed["ats_embeds"])
        aggregator_links.extend({"page": name, **link} for link in parsed["aggregator_links"])
        jobpostings += parsed.get("jobposting_schema_count") or 0

    page_urls = set(sitemaps["pages"]["urls"]) if "pages" in sitemaps else set()
    wp_pages = set(wp_indexes["pages"]["links"]) if "pages" in wp_indexes else set()
    sitemap_pages_covered = page_urls <= wp_pages if page_urls and wp_pages else False
    content_reconciled = {}
    for name, key in (("news", "news"), ("blog", "blog"), ("blog_ja", "blog_ja")):
        if name in sitemaps and key in wp_indexes:
            content_reconciled[name] = set(sitemaps[name]["urls"]) == set(wp_indexes[key]["links"])
    sitemap_declared_urls = set(sitemaps["index"]["urls"]) if "index" in sitemaps else set()
    fetched_sitemaps = {
        PAGE_SITEMAP, NEWS_SITEMAP, BLOG_SITEMAP, BLOG_JA_SITEMAP, POST_ARCHIVE_SITEMAP,
    }
    sitemaps_reconciled = sitemap_declared_urls == fetched_sitemaps

    careers_route_published = bool(
        career_urls or career_items or career_anchors or ats_links or ats_embeds or jobpostings
        or wp_types.get("job_like_rest_bases")
        or any(probe.get("status") == 200 for probe in probes)
    )
    return {
        "careers_route_published": careers_route_published,
        "career_urls": sorted(career_urls),
        "career_items": career_items,
        "career_anchors": career_anchors,
        "external_ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": jobpostings,
        "job_like_rest_bases": wp_types.get("job_like_rest_bases", []),
        "aggregator_links_observed": aggregator_links,
        "page_sitemap_urls": len(page_urls),
        "wp_pages": len(wp_pages),
        "page_sitemap_covered_by_wp_pages": sitemap_pages_covered,
        "content_sitemaps_match_wp_indexes": content_reconciled,
        "declared_sitemaps_match_fetched": sitemaps_reconciled,
        "sitemap_declared": sorted(sitemap_declared_urls),
        "career_path_probe_statuses": {probe["url"]: probe.get("status") for probe in probes},
        "reconciled": bool(sitemap_pages_covered and all(content_reconciled.values())
                           and sitemaps_reconciled),
    }


def _redirect_evidence(response) -> list[dict]:
    return [
        {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
        for item in response.history
    ]


class VoxelaInc(Adapter):
    """Read Voxela's official pages and record that no careers page is published."""

    def _request_log_lines(self) -> int:
        path = self.http.output / "requests.jsonl" if self.http.output else None
        if not path or not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

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
            return None, {
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

    def _finish(self, source_started: float):
        elapsed = max(0.0, time.perf_counter() - source_started)
        self.result.metadata["source_collection_wall_seconds"] = round(elapsed, 6)
        self.result.timings["source_collection_seconds"] = elapsed
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

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": "voxela_inc.official-404.1",
            "official_entry_url": OFFICIAL_ENTRY,
            "official_site_url": OFFICIAL_SITE,
            "employer_name": EMPLOYER_NAME,
            "official_sitemap_index": SITEMAP_INDEX,
            "listing_authority": "none: Voxela's official site publishes no careers page or job listing",
            "detail_method": "none: no Voxela role, requisition or detail route is published",
            "request_scope": (
                "Only the ledger URL and its official-host variants, Voxela's own homepage "
                "(EN/JA), Message/company-profile, Contact, News, Blog and VCare pages, its "
                "robots.txt, the five robots-declared All-in-One-SEO sitemaps, its published "
                "WordPress content indexes (types, pages, posts, news, blog, blog_ja) and its "
                "WordPress search endpoint for 採用/career/jobs/recruit. No guessed ATS tenant, "
                "aggregator or search-result board is queried for membership."
            ),
            "excluded_listing_candidates": [
                {
                    "url": "https://www.instahyre.com/jobs-at-voxela/",
                    "identity": "Instahyre (aggregator)",
                    "reason": (
                        "Only surfaced by a search engine; not linked, embedded, requested or "
                        "redirected to by any voxela.ai page, and Instahyre is an aggregator, so it "
                        "is not company-published membership."
                    ),
                },
                {
                    "url": "https://wellfound.com/company/voxela/jobs",
                    "identity": "Wellfound (aggregator)",
                    "reason": "Same: search-result aggregator profile, no first-party journey.",
                },
                {
                    "url": "https://www.linkedin.com/jobs/voxela-linkedin-jobs",
                    "identity": "LinkedIn job search (aggregator)",
                    "reason": (
                        "A LinkedIn job-search page, not Voxela's board. The official site links only "
                        "three founder profile URLs (identity, not a job listing)."
                    ),
                },
                {
                    "url": "https://www.voxela.ai/",
                    "identity": "ヴォクセル株式会社 (studioVOXEL) / Voxel Group / Voxela Space Factory",
                    "reason": (
                        "Japanese search results for 'Voxela 採用' are name collisions with unrelated "
                        "companies (studioVoxel, Voxel Group, Voxela Space Factory); not Voxela, Inc."
                    ),
                },
            ],
        })

        # 1. The ledger URL: reviewed state is the company's own 404.
        _, entry_record = await self._fetch(OFFICIAL_ENTRY, "official_careers_entry")
        try:
            body = entry_record.get("body")
            if body is None:
                raise ValueError("official careers entry response body is unavailable")
            entry = parse_entry_response(
                status_code=entry_record["status_code"],
                final_url=entry_record.get("final_url") or "",
                html=body.decode("utf-8", errors="replace"),
            )
        except Exception as exc:
            self.result.metadata["initial_entry"] = self._evidence_from_record(entry_record)
            self.result.errors.append(f"voxela_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["observed_journey"] = {
                "conclusion": "The ledger careers URL could not be validated against the reviewed journey."
            }
            return self._finish(source_started)

        self.result.metadata["initial_entry"] = {
            **self._evidence_from_record(entry_record),
            "kind": entry["kind"],
            "canonical": entry.get("canonical"),
            "title": entry.get("title"),
            "h1": entry.get("h1"),
            "robots_meta": entry.get("robots_meta"),
            "body": entry.get("body"),
            "jobposting_schema_count": entry.get("jobposting_schema_count"),
            "career_links": entry.get("career_links"),
            "ats_links": entry.get("ats_links"),
            "ats_embeds": entry.get("ats_embeds"),
            "external_hosts": entry.get("external_hosts"),
        }
        self.result.metadata["observed_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "final_url": entry["response_url"],
            "status_code": entry["status_code"],
            "title": entry.get("title"),
            "h1": entry.get("h1"),
            "robots_meta": entry.get("robots_meta"),
            "job_listing_candidates": entry.get("career_links") or [],
            "external_ats_links": entry.get("ats_links") or [],
            "ats_embeds": entry.get("ats_embeds") or [],
            "job_posting_schema_count": entry.get("jobposting_schema_count") or 0,
            "visible_text": entry.get("body"),
        }
        if entry["kind"] != "official_404":
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The ledger careers URL now serves a published page instead of the reviewed 404; "
                "the new journey must be inspected before any membership is collected."
            )
            return self._finish(source_started)
        if has_listing_signal(entry):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The 404 response now carries a job-listing signal; the journey must be inspected "
                "before any membership is collected."
            )
            return self._finish(source_started)

        # 2. Official marketing/company pages.
        pages: dict[str, dict] = {}
        for url, name, purpose in (
            (OFFICIAL_SITE, "home", "official_homepage"),
            (HOME_JA, "home_ja", "official_homepage_ja"),
            (MESSAGE, "message", "company_profile"),
            (MESSAGE_JA, "message_ja", "company_profile_ja"),
            (CONTACT, "contact", "contact_page"),
            (NEWS, "news", "news_index"),
            (BLOG, "blog", "blog_index"),
            (VCARE, "vcare", "product_page"),
        ):
            _, record = await self._fetch(url, purpose)
            try:
                if record.get("body") is None:
                    raise ValueError(f"{name} response body is unavailable")
                parsed = parse_page(record["body"].decode("utf-8", errors="replace"),
                                    record.get("final_url") or url)
            except Exception as exc:
                self.result.metadata[name] = self._evidence_from_record(record)
                self.result.errors.append(f"voxela_{name}_failed: {type(exc).__name__}: {exc}")
                return self._finish(source_started)
            pages[name] = parsed
            self.result.metadata[name] = {
                **self._evidence_from_record(record),
                "canonical": parsed["canonical"],
                "title": parsed["title"],
                "h1": parsed["h1"],
                "anchor_count": parsed["anchor_count"],
                "internal_paths": sorted(set(parsed["internal_paths"])),
                "external_hosts": parsed["external_hosts"],
                "job_listing_candidates": parsed["career_links"],
                "external_ats_links": parsed["ats_links"],
                "ats_embeds": parsed["ats_embeds"],
                "job_posting_schema_count": parsed["jobposting_schema_count"],
                "body": parsed["body"],
            }
            if has_listing_signal(parsed):
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    f"The official {name} page now links or embeds a careers/job listing; the new "
                    "journey must be inspected before any membership is collected."
                )
                return self._finish(source_started)

        # 3. robots.txt and the robots-declared sitemaps.
        _, robots_record = await self._fetch(ROBOTS, "robots")
        try:
            if robots_record.get("body") is None:
                raise ValueError("robots.txt response body is unavailable")
            robots = parse_robots(robots_record["body"].decode("utf-8", errors="replace"))
            if not robots["declares_official_sitemap"]:
                raise ValueError("robots.txt no longer declares the reviewed sitemap index")
            if robots["career_tokens"]:
                raise ValueError(f"robots.txt now carries careers tokens: {robots['career_tokens']}")
        except Exception as exc:
            self.result.metadata["robots"] = self._evidence_from_record(robots_record)
            self.result.errors.append(f"voxela_robots_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["robots"] = {
            **self._evidence_from_record(robots_record),
            "sitemaps": robots["sitemaps"],
            "career_tokens": robots["career_tokens"],
        }

        sitemaps: dict[str, dict] = {}
        for url, name in (
            (SITEMAP_INDEX, "index"),
            (PAGE_SITEMAP, "pages"),
            (NEWS_SITEMAP, "news"),
            (BLOG_SITEMAP, "blog"),
            (BLOG_JA_SITEMAP, "blog_ja"),
            (POST_ARCHIVE_SITEMAP, "post_archive"),
        ):
            _, record = await self._fetch(url, f"sitemap_{name}")
            try:
                if record.get("body") is None:
                    raise ValueError(f"{name} sitemap response body is unavailable")
                parsed = parse_sitemap(record["body"].decode("utf-8", errors="replace"))
            except Exception as exc:
                self.result.metadata[f"sitemap_{name}"] = self._evidence_from_record(record)
                self.result.errors.append(f"voxela_sitemap_{name}_failed: {type(exc).__name__}: {exc}")
                return self._finish(source_started)
            sitemaps[name] = parsed
            self.result.metadata[f"sitemap_{name}"] = {
                **self._evidence_from_record(record),
                "kind": parsed["kind"],
                "url_count": parsed["url_count"],
                "hosts": parsed["hosts"],
                "career_like_urls": parsed["career_like_urls"],
                "foreign_host_urls": parsed["foreign_host_urls"],
            }
            if parsed["career_like_urls"] or parsed["foreign_host_urls"]:
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    f"The official {name} sitemap now publishes a careers/job or foreign-host URL; "
                    "the new journey must be inspected before any membership is collected."
                )
                return self._finish(source_started)
        if len(sitemaps["index"]["urls"]) != EXPECTED_SITEMAPS:
            self.result.warnings.append("sitemap_index_size_changed")

        # 4. Every published WordPress content index.
        wp_indexes: dict[str, dict] = {}
        for url, name in ((WP_PAGES, "pages"), (WP_POSTS, "posts"), (WP_NEWS, "news"),
                          (WP_BLOG, "blog"), (WP_BLOG_JA, "blog_ja")):
            _, record = await self._fetch(url, f"wp_{name}")
            try:
                if record.get("body") is None:
                    raise ValueError(f"WordPress {name} response body is unavailable")
                parsed = parse_wp_index(json.loads(record["body"]), name)
            except Exception as exc:
                self.result.metadata[f"wp_{name}"] = self._evidence_from_record(record)
                self.result.errors.append(f"voxela_wp_{name}_failed: {type(exc).__name__}: {exc}")
                return self._finish(source_started)
            wp_indexes[name] = parsed
            self.result.metadata[f"wp_{name}"] = {
                **self._evidence_from_record(record),
                "count": parsed["count"],
                "ids_unique": parsed["ids_unique"],
                "career_like_items": parsed["career_like_items"],
                "foreign_host_links": parsed["foreign_host_links"],
                "items": [
                    {key: item[key] for key in ("id", "slug", "link", "title", "status")}
                    for item in parsed["items"]
                ],
            }
            if parsed["career_like_items"] or parsed["foreign_host_links"]:
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    f"The WordPress {name} index now publishes a careers/job or foreign-host URL; "
                    "the new journey must be inspected before any membership is collected."
                )
                return self._finish(source_started)

        _, types_record = await self._fetch(WP_TYPES, "wp_types")
        try:
            if types_record.get("body") is None:
                raise ValueError("WordPress types response body is unavailable")
            wp_types = parse_wp_types(json.loads(types_record["body"]))
        except Exception as exc:
            self.result.metadata["wp_types"] = self._evidence_from_record(types_record)
            self.result.errors.append(f"voxela_wp_types_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["wp_types"] = {
            **self._evidence_from_record(types_record),
            **wp_types,
        }
        if wp_types["job_like_rest_bases"]:
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "WordPress now registers a job/career content type; the journey must be inspected "
                "before any membership is collected."
            )
            return self._finish(source_started)

        # 5. WordPress search for careers vocabulary (recorded, not membership).
        searches = {}
        for term in ("採用", "career", "jobs", "recruit"):
            url = f"{WP_SEARCH}?search={term}"
            _, record = await self._fetch(url, f"wp_search_{term}")
            try:
                if record.get("body") is None:
                    raise ValueError(f"WordPress search '{term}' response body is unavailable")
                parsed = parse_wp_search(json.loads(record["body"]))
            except Exception as exc:
                searches[term] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            searches[term] = {**self._evidence_from_record(record), **parsed}
        self.result.metadata["wp_search"] = searches

        # 6. Bounded recheck of the obvious first-party careers routes.
        probes, probe_evidence = [], {}
        for path in CAREER_PATH_RECHECKS:
            url = "https://www.voxela.ai" + path
            _, record = await self._fetch(url, f"career_path_probe:{path}")
            status = record.get("status_code")
            probe = {"url": url, "status": status,
                     "final_url": record.get("final_url"),
                     "body_bytes": record.get("html_bytes"),
                     "html_sha256": record.get("html_sha256")}
            probes.append(probe)
            probe_evidence[url] = probe
            if status != 404:
                self.result.warnings.append(f"career_path_not_404:{path}:{status}")
        self.result.metadata["career_path_probes"] = probe_evidence

        # 7. Reconcile the published inventory and decide membership.
        membership = audit_journey(pages=pages, sitemaps=sitemaps,
                                   wp_indexes=wp_indexes, wp_types=wp_types, probes=probes)
        membership.update({
            "careers_page_http_status": entry["status_code"],
            "careers_page_title": entry.get("title"),
            "careers_page_h1": entry.get("h1"),
            "careers_page_robots": entry.get("robots_meta"),
            "careers_page_body": entry.get("body"),
            "listing_rows": 0,
            "listing_url": None,
            "page_sitemap_urls": membership["page_sitemap_urls"],
            "career_pages_published": len(membership["career_urls"]),
        })
        membership["note"] = (
            "Membership is empty because Voxela's official site publishes no careers page, job "
            "listing, ATS link/embed or JobPosting schema on its own journey, not because an "
            "enumerated listing was empty. Aggregator results and name collisions were excluded."
        )
        self.result.metadata["membership"] = membership
        self.result.metadata["observed_journey"]["membership"] = {
            "careers_route_published": membership["careers_route_published"],
            "career_urls": membership["career_urls"],
            "career_anchors": membership["career_anchors"],
            "external_ats_links": membership["external_ats_links"],
            "ats_embeds": membership["ats_embeds"],
            "job_posting_schema_count": membership["job_posting_schema_count"],
            "reconciled": membership["reconciled"],
        }
        if membership["careers_route_published"]:
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official site now publishes a careers signal; the journey must be inspected "
                "before any membership is collected."
            )
            return self._finish(source_started)

        self.result.errors.append("no_public_careers_page")
        self.result.metadata["observed_journey"]["conclusion"] = (
            "Voxela's official site publishes no careers, jobs, recruiting or ATS journey: the "
            "ledger URL is the site's own 404, and its robots-declared sitemaps plus every "
            "published WordPress content index contain no careers route, ATS link/embed or "
            "JobPosting schema, so there is no company-scoped listing to collect."
        )
        return self._finish(source_started)
