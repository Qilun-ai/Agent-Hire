"""MPL's official careers journey (reviewed 2026-11-07).

The ledger entry ``https://www.mplgaming.com/careers/`` is **not published** on
MPL's own WordPress site: the server answers ``404`` with the site's own
"404 Not Found | MPL" page (h1 "No Results Found", recent blog posts in the
sidebar).  The official homepage navigation, the About/Life at MPL pages, the
robots-declared WordPress sitemaps and the WordPress page/post/navigation REST
indexes publish no careers or jobs route and link/embed no ATS.  The About page
identifies MPL as part of **M-League**, the parent group.

A guessed Darwinbox tenant ``mpl.darwinbox.in`` is branded **M-League** (parent
group, tenant 318) and exposed four parent-group roles when probed.  It is not
linked, embedded, requested or redirected to by MPL's own pages, and its board
name is the parent group, not MPL: per the project contract a guessed tenant or
a parent-company-wide board is not proof of MPL scope, so it is not collected.
Other guessed tenants are name collisions (``apply.workable.com/mpl`` is
Marksans Pharma) or empty.  The only MPL-branded roles observed were two
LinkedIn aggregator postings, which are not first-party membership.

This adapter therefore records the absence explicitly instead of returning a
successful zero-job listing.  It fails closed: if the ledger URL starts serving
a real page, or the official site grows a careers link/page/ATS/JobPosting, it
reports ``unreviewed_careers_journey_detected`` so the new journey is inspected
before any membership is collected.

The shared HTTP client raises on 4xx, so the 404 body is read back from the
client's own content-addressed response archive (the same pattern used by the
other 404 reviews).  No unarchived request is made.
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


OFFICIAL_ENTRY = "https://www.mplgaming.com/careers/"
OFFICIAL_SITE = "https://www.mplgaming.com/"
ABOUT_PAGE = "https://www.mplgaming.com/about/"
LIFE_AT_MPL = "https://www.mplgaming.com/life-at-mpl/"
ROBOTS = "https://www.mplgaming.com/robots.txt"
SITEMAP_INDEX = "https://www.mplgaming.com/wp-sitemap.xml"
PAGE_SITEMAP = "https://www.mplgaming.com/wp-sitemap-posts-page-1.xml"
POST_SITEMAP = "https://www.mplgaming.com/wp-sitemap-posts-post-1.xml"
WP_PAGES = "https://www.mplgaming.com/wp-json/wp/v2/pages?per_page=100&_fields=id,slug,link,title,modified"
WP_POSTS = "https://www.mplgaming.com/wp-json/wp/v2/posts?per_page=100&_fields=id,slug,link,title,modified"
WP_NAVIGATION = "https://www.mplgaming.com/wp-json/wp/v2/navigation?per_page=100"
LIVE_HOME = "https://www.mpl.live/"
LIVE_CAREERS = "https://www.mpl.live/careers"

OFFICIAL_HOSTS = frozenset({"mplgaming.com", "www.mplgaming.com"})
LIVE_HOSTS = frozenset({"mpl.live", "www.mpl.live"})
EXPECTED_404_TITLE = "404 Not Found | MPL"
EXPECTED_404_H1 = "No Results Found"
EXPECTED_HOME_CANONICAL = "https://www.mplgaming.com/"
EXPECTED_HOME_TITLE = "MPL | MPL"
EXPECTED_LIFE_CANONICAL = "https://www.mplgaming.com/life-at-mpl/"
EXPECTED_LIFE_TITLE = "Life at MPL | MPL"
EXPECTED_LIVE_CANONICAL = "https://www.mpl.live/"
EXPECTED_LIVE_TITLE = "Play Games on MPL"

# A real external job board/ATS host.  None is linked by MPL; the pattern exists
# so a future change is detected and reviewed, never scraped by guess.
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
# Aggregator hosts only count as a listing signal on their job-search paths;
# a LinkedIn *profile* link is company identity, not a board.
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:linkedin\.com|indeed\.com|glassdoor\.com|instahyre\.com|"
    r"wellfound\.com|cutshort\.io|naukri\.com)$"
)
AGGREGATOR_JOB_PATH = re.compile(
    r"(?i)^/(?:jobs?|careers?|vacanc|job-search|company/[^/]+/jobs)"
)
# Career/job *paths* on the official site.  Segment-anchored so blog slugs such
# as /server-authoritative-games/ never count as a careers page.
CAREER_PATH = re.compile(
    r"(?i)^/(?:careers?|jobs?|roles?|open-roles|open-positions|join-us|"
    r"we-are-hiring|hiring)(?:/|$)"
)

# Evidence for the scope decision.  These are NOT requested by the adapter.
EXCLUDED_CANDIDATES = [
    {
        "url": "https://mpl.darwinbox.in/ms/candidate/careers",
        "identity": "M-League (Darwinbox tenant subdomain 'mpl', tenant_id 318, company_name 'M-League')",
        "reason": (
            "Guessed ATS tenant. Not linked/embedded/requested/redirected to by any MPL page. "
            "Its API reports company_name 'M-League' (the parent group named on MPL's own About "
            "page) and it exposed four parent-group roles with no MPL brand scope, so it is a "
            "parent-company-wide board and is not used for MPL membership."
        ),
        "observed_roles": 4,
        "evidence": "companies/mpl/source/parent-group/",
    },
    {
        "url": "https://apply.workable.com/mpl/",
        "identity": "Marksans Pharma",
        "reason": (
            "Name collision: the board is branded 'MPL' but its own llms.txt gives the company "
            "website as marksanspharma.com and it had 0 openings; not Mobile Premier League."
        ),
    },
    {
        "url": "https://www.linkedin.com/jobs/mobile-premier-league-%28mpl%29-jobs-worldwide?f_C=14566676",
        "identity": "LinkedIn (aggregator)",
        "reason": (
            "Two MPL-branded roles were visible ('Short-Form Content Creator - Mobile Card Game', "
            "'Short Form Content Creator Blast+'), but LinkedIn is an aggregator and not the "
            "company's first-party membership; not used."
        ),
        "observed_roles": 2,
        "evidence": "companies/mpl/source/aggregator/",
    },
]


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u2019", "'").replace("\u2018", "'")).strip()


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
        "visible_text": text,
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


def parse_entry_response(*, status_code: int, final_url: str, html: str) -> dict:
    """Validate the ledger URL against the reviewed 404 journey."""
    parts = urlsplit(final_url)
    if (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"official careers entry redirected to an unexpected host: {final_url}")
    if parts.path.rstrip("/") != "/careers":
        raise ValueError(f"official careers entry redirected to an unexpected path: {final_url}")
    parsed = page_signals(html, final_url)
    parsed["status_code"] = status_code
    parsed["html_chars"] = len(html)
    parsed["html_sha256"] = sha256(html.encode("utf-8")).hexdigest()
    if status_code == 404:
        if parsed["title"] != EXPECTED_404_TITLE:
            raise ValueError(f"official 404 title changed: {parsed['title']!r}")
        if parsed["h1"] != [EXPECTED_404_H1]:
            raise ValueError(f"official 404 heading changed: {parsed['h1']!r}")
        if "No Results Found" not in parsed["visible_text"]:
            raise ValueError("official 404 body no longer contains its reviewed not-found text")
        parsed["kind"] = "official_404"
    elif status_code == 200:
        parsed["kind"] = "published_page"
    else:
        raise ValueError(f"official careers URL returned unreviewed status {status_code}")
    return parsed


def parse_homepage(html: str, response_url: str = OFFICIAL_SITE) -> dict:
    parts = urlsplit(response_url)
    if (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"official homepage redirected to an unexpected host: {response_url}")
    if parts.path not in ("", "/"):
        raise ValueError(f"official homepage response came from an unexpected path: {response_url}")
    parsed = page_signals(html, response_url)
    if parsed["canonical"] != EXPECTED_HOME_CANONICAL:
        raise ValueError(f"official homepage canonical changed: {parsed['canonical']!r}")
    if parsed["title"] != EXPECTED_HOME_TITLE:
        raise ValueError(f"official homepage title changed: {parsed['title']!r}")
    parsed["internal_paths"] = sorted({
        urlsplit(item["url"]).path for item in parsed["anchors"]
        if (urlsplit(item["url"]).hostname or "").lower() in OFFICIAL_HOSTS
    })
    return parsed


def parse_life_page(html: str, response_url: str = LIFE_AT_MPL) -> dict:
    parts = urlsplit(response_url)
    if (parts.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"Life at MPL redirected to an unexpected host: {response_url}")
    if parts.path.rstrip("/") != "/life-at-mpl":
        raise ValueError(f"Life at MPL came from an unexpected path: {response_url}")
    parsed = page_signals(html, response_url)
    if parsed["canonical"] != EXPECTED_LIFE_CANONICAL:
        raise ValueError(f"Life at MPL canonical changed: {parsed['canonical']!r}")
    if parsed["title"] != EXPECTED_LIFE_TITLE:
        raise ValueError(f"Life at MPL title changed: {parsed['title']!r}")
    if "Guiding Values" not in parsed["visible_text"]:
        raise ValueError("Life at MPL no longer contains its reviewed culture content")
    return parsed


def parse_sitemap(xml: str) -> dict:
    urls = [value.strip() for value in re.findall(r"<loc>(.*?)</loc>", xml)]
    if not urls:
        raise ValueError("official sitemap contains no <loc> URLs")
    foreign = [url for url in urls if (urlsplit(url).hostname or "").lower() not in OFFICIAL_HOSTS]
    career_like = [url for url in urls if CAREER_PATH.match(urlsplit(url).path)]
    return {
        "url_count": len(urls),
        "hosts": sorted({(urlsplit(url).hostname or "").lower() for url in urls}),
        "foreign_host_urls": foreign,
        "career_like_urls": career_like,
        "urls": urls,
    }


def parse_wp_index(payload, kind: str) -> dict:
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"WordPress {kind} index is not a non-empty list")
    items, foreign = [], []
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("link"), str):
            raise ValueError(f"WordPress {kind} index row is missing its link")
        link = row["link"]
        if (urlsplit(link).hostname or "").lower() not in OFFICIAL_HOSTS:
            foreign.append(link)
        title = row.get("title")
        title = title.get("rendered") if isinstance(title, dict) else title
        items.append({
            "id": row.get("id"),
            "slug": row.get("slug"),
            "link": link,
            "title": normalize_text(title),
            "modified": row.get("modified"),
        })
    career_like = [item for item in items if CAREER_PATH.match(urlsplit(item["link"]).path)]
    return {
        "kind": kind,
        "count": len(items),
        "items": items,
        "career_like_items": career_like,
        "foreign_host_links": foreign,
    }


def parse_navigation(payload) -> dict:
    if not isinstance(payload, list) or not payload:
        raise ValueError("WordPress navigation index is not a non-empty list")
    links = []
    for row in payload:
        content = row.get("content") if isinstance(row, dict) else None
        rendered = content.get("rendered") if isinstance(content, dict) else ""
        soup = BeautifulSoup(rendered or "", "lxml")
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            links.append({
                "text": normalize_text(anchor.get_text(" ", strip=True)),
                "href": href,
                "url": href,
            })
    if not links:
        raise ValueError("WordPress navigation renders no links")
    career_like = [link for link in links if CAREER_PATH.match(urlsplit(link["url"]).path)]
    ats_links = [
        link for link in links
        if ATS_HOST.search(_host(link["url"]))
        or (AGGREGATOR_HOST.search(_host(link["url"]))
            and AGGREGATOR_JOB_PATH.match(urlsplit(link["url"]).path))
    ]
    return {
        "link_count": len(links),
        "links": links,
        "career_like_links": career_like,
        "external_ats_links": ats_links,
    }


def parse_robots(text: str) -> dict:
    sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)\s*$", text)
    career_tokens = [
        line for line in text.splitlines()
        if re.search(r"(?i)career|jobs?|hiring", line)
    ]
    return {
        "sitemaps": sitemaps,
        "declares_official_sitemap": SITEMAP_INDEX in sitemaps,
        "career_tokens": career_tokens,
    }


def parse_live_home(html: str, response_url: str = LIVE_HOME) -> dict:
    parts = urlsplit(response_url)
    if (parts.hostname or "").lower() not in LIVE_HOSTS:
        raise ValueError(f"MPL consumer homepage redirected to an unexpected host: {response_url}")
    if parts.path not in ("", "/"):
        raise ValueError(f"MPL consumer homepage came from an unexpected path: {response_url}")
    parsed = page_signals(html, response_url)
    if parsed["canonical"] != EXPECTED_LIVE_CANONICAL:
        raise ValueError(f"MPL consumer homepage canonical changed: {parsed['canonical']!r}")
    if parsed["title"] != EXPECTED_LIVE_TITLE:
        raise ValueError(f"MPL consumer homepage title changed: {parsed['title']!r}")
    return parsed


def parse_live_404(*, status_code: int, final_url: str, html: str) -> dict:
    parts = urlsplit(final_url)
    if (parts.hostname or "").lower() not in LIVE_HOSTS:
        raise ValueError(f"MPL consumer /careers redirected to an unexpected host: {final_url}")
    if parts.path.rstrip("/") != "/careers":
        raise ValueError(f"MPL consumer /careers came from an unexpected path: {final_url}")
    parsed = page_signals(html, final_url)
    parsed["status_code"] = status_code
    parsed["html_sha256"] = sha256(html.encode("utf-8")).hexdigest()
    rendered_404 = "couldn't find that page" in normalize_text(parsed["visible_text"]).lower()
    if status_code == 404 and ("__next_error__" in html or rendered_404):
        parsed["kind"] = "nextjs_404"
    elif status_code == 200:
        parsed["kind"] = "published_page"
    else:
        raise ValueError(f"MPL consumer /careers returned unreviewed status {status_code}")
    return parsed


def _entry_evidence(parsed: dict) -> dict:
    return {
        "kind": parsed["kind"],
        "final_url": parsed["response_url"],
        "status_code": parsed["status_code"],
        "title": parsed["title"],
        "h1": parsed["h1"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
    }


def _redirect_evidence(response) -> list[dict]:
    return [
        {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
        for item in response.history
    ]


class MPL(Adapter):
    """Read MPL's official pages and record that no careers page is published."""

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
        self.result.metadata["source_collection_wall_seconds"] = elapsed
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
            "verification_revision": "mpl.official-404.1",
            "official_entry_url": OFFICIAL_ENTRY,
            "official_site_url": OFFICIAL_SITE,
            "about_page_url": ABOUT_PAGE,
            "life_at_mpl_url": LIFE_AT_MPL,
            "official_sitemap_index": SITEMAP_INDEX,
            "official_page_sitemap": PAGE_SITEMAP,
            "official_post_sitemap": POST_SITEMAP,
            "listing_authority": "none: MPL's official site publishes no careers page or job listing",
            "detail_method": "none: no MPL role, requisition or detail route is published",
            "request_scope": (
                "Only the ledger URL, MPL's official homepage/About/Life pages, its robots.txt, "
                "its WordPress sitemaps and WordPress page/post/navigation REST indexes, and its "
                "consumer mpl.live homepage + /careers route. No guessed ATS tenant, parent-group "
                "board or aggregator is queried for membership."
            ),
            "excluded_listing_candidates": EXCLUDED_CANDIDATES,
            "parent_group_note": (
                "MPL's own About page states 'MPL is part of M-League'. The unlinked Darwinbox "
                "tenant mpl.darwinbox.in reports company_name 'M-League' (tenant 318) and four "
                "parent-group roles; it is not MPL-scoped and is not used."
            ),
        })

        # 1. The ledger URL and its reviewed 404.
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
            self.result.errors.append(f"mpl_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["observed_journey"] = {
                "conclusion": "The ledger careers URL could not be validated against the reviewed journey."
            }
            return self._finish(source_started)

        self.result.metadata["initial_entry"] = {
            **self._evidence_from_record(entry_record),
            "canonical": entry["canonical"],
            "title": entry["title"],
            "h1": entry["h1"],
            "redirects": entry_record.get("redirects") or [],
        }
        self.result.metadata["observed_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
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
            return self._finish(source_started)
        if has_listing_signal(entry):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The 404 response now carries a job-listing signal; the journey must be inspected "
                "before any membership is collected."
            )
            return self._finish(source_started)

        # 2. The official homepage navigation.
        _, home_record = await self._fetch(OFFICIAL_SITE, "official_homepage")
        try:
            if home_record.get("body") is None:
                raise ValueError("official homepage response body is unavailable")
            home = parse_homepage(home_record["body"].decode("utf-8", errors="replace"),
                                  home_record.get("final_url") or "")
        except Exception as exc:
            self.result.metadata["official_site"] = self._evidence_from_record(home_record)
            self.result.errors.append(f"mpl_official_site_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)

        self.result.metadata["official_site"] = {
            **self._evidence_from_record(home_record),
            "canonical": home["canonical"],
            "title": home["title"],
            "h1": home["h1"],
            "anchor_count": home["anchor_count"],
            "internal_paths": home["internal_paths"],
            "job_listing_candidates": home["listing_candidates"],
            "external_ats_links": home["external_ats_links"],
            "ats_embeds": home["ats_embeds"],
            "job_posting_schema_count": home["job_posting_schema_count"],
            "normalized_text_chars": home["normalized_text_chars"],
            "normalized_text_sha256": home["normalized_text_sha256"],
        }
        if has_listing_signal(home):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official homepage now links or embeds a careers/job listing; the new journey "
                "must be inspected before any membership is collected."
            )
            return self._finish(source_started)

        # 3. The culture page ("Life at MPL").
        _, life_record = await self._fetch(LIFE_AT_MPL, "life_at_mpl")
        try:
            if life_record.get("body") is None:
                raise ValueError("Life at MPL response body is unavailable")
            life = parse_life_page(life_record["body"].decode("utf-8", errors="replace"),
                                   life_record.get("final_url") or "")
        except Exception as exc:
            self.result.metadata["life_at_mpl"] = self._evidence_from_record(life_record)
            self.result.errors.append(f"mpl_life_page_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)

        self.result.metadata["life_at_mpl"] = {
            **self._evidence_from_record(life_record),
            "canonical": life["canonical"],
            "title": life["title"],
            "anchor_count": life["anchor_count"],
            "job_listing_candidates": life["listing_candidates"],
            "external_ats_links": life["external_ats_links"],
            "ats_embeds": life["ats_embeds"],
            "job_posting_schema_count": life["job_posting_schema_count"],
            "normalized_text_chars": life["normalized_text_chars"],
            "normalized_text_sha256": life["normalized_text_sha256"],
        }
        if has_listing_signal(life):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The Life at MPL page now links or embeds a careers/job listing; the new journey "
                "must be inspected before any membership is collected."
            )
            return self._finish(source_started)

        # 4. robots.txt and the complete published WordPress page/post indexes.
        _, robots_record = await self._fetch(ROBOTS, "robots")
        try:
            if robots_record.get("body") is None:
                raise ValueError("robots.txt response body is unavailable")
            robots = parse_robots(robots_record["body"].decode("utf-8", errors="replace"))
            if not robots["declares_official_sitemap"]:
                raise ValueError("robots.txt no longer declares the reviewed WordPress sitemap")
        except Exception as exc:
            self.result.metadata["robots"] = self._evidence_from_record(robots_record)
            self.result.errors.append(f"mpl_robots_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["robots"] = {
            **self._evidence_from_record(robots_record),
            "sitemaps": robots["sitemaps"],
            "career_tokens": robots["career_tokens"],
        }

        sitemap_records = {}
        for url, name, kind in [(SITEMAP_INDEX, "sitemap_index", "index"),
                                (PAGE_SITEMAP, "page_sitemap", "pages"),
                                (POST_SITEMAP, "post_sitemap", "posts")]:
            _, record = await self._fetch(url, name)
            try:
                if record.get("body") is None:
                    raise ValueError(f"{name} response body is unavailable")
                parsed = parse_sitemap(record["body"].decode("utf-8", errors="replace"))
            except Exception as exc:
                self.result.metadata[name] = self._evidence_from_record(record)
                self.result.errors.append(f"mpl_{name}_failed: {type(exc).__name__}: {exc}")
                return self._finish(source_started)
            sitemap_records[name] = parsed
            self.result.metadata[name] = {
                **self._evidence_from_record(record),
                "url_count": parsed["url_count"],
                "hosts": parsed["hosts"],
                "foreign_host_urls": parsed["foreign_host_urls"],
                "career_like_urls": parsed["career_like_urls"],
            }
            if parsed["career_like_urls"] or parsed["foreign_host_urls"]:
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    f"The official {kind} sitemap now publishes a careers/job or foreign-host URL; "
                    "the new journey must be inspected before any membership is collected."
                )
                return self._finish(source_started)

        wp_records = {}
        for url, name, kind in [(WP_PAGES, "wp_pages", "pages"),
                                (WP_POSTS, "wp_posts", "posts")]:
            _, record = await self._fetch(url, name)
            try:
                if record.get("body") is None:
                    raise ValueError(f"WordPress {kind} response body is unavailable")
                parsed = parse_wp_index(json.loads(record["body"]), kind)
            except Exception as exc:
                self.result.metadata[name] = self._evidence_from_record(record)
                self.result.errors.append(f"mpl_{name}_failed: {type(exc).__name__}: {exc}")
                return self._finish(source_started)
            wp_records[name] = parsed
            self.result.metadata[name] = {
                **self._evidence_from_record(record),
                "count": parsed["count"],
                "items": parsed["items"],
                "career_like_items": parsed["career_like_items"],
                "foreign_host_links": parsed["foreign_host_links"],
            }
            if parsed["career_like_items"] or parsed["foreign_host_links"]:
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    f"The WordPress {kind} index now publishes a careers/job or foreign-host URL; "
                    "the new journey must be inspected before any membership is collected."
                )
                return self._finish(source_started)

        _, nav_record = await self._fetch(WP_NAVIGATION, "wp_navigation")
        try:
            if nav_record.get("body") is None:
                raise ValueError("WordPress navigation response body is unavailable")
            navigation = parse_navigation(json.loads(nav_record["body"]))
        except Exception as exc:
            self.result.metadata["wp_navigation"] = self._evidence_from_record(nav_record)
            self.result.errors.append(f"mpl_wp_navigation_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["wp_navigation"] = {
            **self._evidence_from_record(nav_record),
            "link_count": navigation["link_count"],
            "links": navigation["links"],
            "career_like_links": navigation["career_like_links"],
            "external_ats_links": navigation["external_ats_links"],
        }
        if navigation["career_like_links"] or navigation["external_ats_links"]:
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official WordPress navigation now publishes a careers/job or ATS link; the "
                "new journey must be inspected before any membership is collected."
            )
            return self._finish(source_started)

        # 5. The consumer mpl.live domain: home + the /careers route.
        _, live_home_record = await self._fetch(LIVE_HOME, "live_home")
        try:
            if live_home_record.get("body") is None:
                raise ValueError("MPL consumer homepage response body is unavailable")
            live_home = parse_live_home(live_home_record["body"].decode("utf-8", errors="replace"),
                                        live_home_record.get("final_url") or "")
        except Exception as exc:
            self.result.metadata["live_home"] = self._evidence_from_record(live_home_record)
            self.result.errors.append(f"mpl_live_home_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["live_home"] = {
            **self._evidence_from_record(live_home_record),
            "canonical": live_home["canonical"],
            "title": live_home["title"],
            "anchor_count": live_home["anchor_count"],
            "job_listing_candidates": live_home["listing_candidates"],
            "external_ats_links": live_home["external_ats_links"],
            "ats_embeds": live_home["ats_embeds"],
            "job_posting_schema_count": live_home["job_posting_schema_count"],
        }
        if has_listing_signal(live_home):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The consumer mpl.live homepage now links or embeds a careers/job listing; the "
                "new journey must be inspected before any membership is collected."
            )
            return self._finish(source_started)

        _, live_careers_record = await self._fetch(LIVE_CAREERS, "live_careers")
        try:
            if live_careers_record.get("body") is None:
                raise ValueError("MPL consumer /careers response body is unavailable")
            live_careers = parse_live_404(
                status_code=live_careers_record["status_code"],
                final_url=live_careers_record.get("final_url") or "",
                html=live_careers_record["body"].decode("utf-8", errors="replace"),
            )
        except Exception as exc:
            self.result.metadata["live_careers"] = self._evidence_from_record(live_careers_record)
            self.result.errors.append(f"mpl_live_careers_failed: {type(exc).__name__}: {exc}")
            return self._finish(source_started)
        self.result.metadata["live_careers"] = {
            **self._evidence_from_record(live_careers_record),
            "kind": live_careers["kind"],
            "title": live_careers["title"],
            "job_listing_candidates": live_careers["listing_candidates"],
            "job_posting_schema_count": live_careers["job_posting_schema_count"],
        }
        if live_careers["kind"] != "nextjs_404" or has_listing_signal(live_careers):
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The consumer mpl.live /careers route now serves a published page or listing; the "
                "new journey must be inspected before any membership is collected."
            )
            return self._finish(source_started)

        # 6. Exact final recheck of the same ledger URL.
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
            )
            self.result.metadata["final_recheck"] = {
                **self._evidence_from_record(recheck_record),
                "canonical": recheck["canonical"],
                "title": recheck["title"],
                "h1": recheck["h1"],
                "normalized_text_sha256": recheck["normalized_text_sha256"],
            }
        except Exception as exc:
            self.result.errors.append(f"mpl_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["final_recheck_seconds"] = max(
                0.0, time.perf_counter() - recheck_started)

        matches = recheck is not None and _entry_evidence(entry) == _entry_evidence(recheck)
        if recheck is not None:
            self.result.metadata["final_recheck_matches_initial"] = matches
            self.result.metadata["final_recheck_html_sha256_matches"] = (
                self.result.metadata["initial_entry"]["html_sha256"]
                == self.result.metadata["final_recheck"]["html_sha256"]
            )
            self.result.metadata["final_recheck_normalized_text_matches"] = (
                entry["normalized_text_sha256"] == recheck["normalized_text_sha256"]
            )
            if not matches:
                self.result.errors.append("mpl_ledger_entry_changed_between_requests")
            if recheck["kind"] != "official_404" or has_listing_signal(recheck):
                self.result.errors.append("unreviewed_careers_journey_detected")

        if matches and not self.result.errors:
            self.result.errors.append("no_public_careers_page")

        self.result.metadata["membership"] = {
            "advertised_jobs": None,
            "listing_rows": 0,
            "careers_page_http_status": entry["status_code"],
            "careers_page_title": entry["title"],
            "careers_page_h1": entry["h1"],
            "careers_route_published": False,
            "page_sitemap_urls": sitemap_records["page_sitemap"]["url_count"],
            "post_sitemap_urls": sitemap_records["post_sitemap"]["url_count"],
            "wp_pages": wp_records["wp_pages"]["count"],
            "wp_posts": wp_records["wp_posts"]["count"],
            "wp_navigation_links": navigation["link_count"],
            "career_pages_published": 0,
            "job_listing_candidates": [],
            "external_ats_links": [],
            "ats_embeds": [],
            "job_posting_schema_count": 0,
            "reconciled": True,
            "note": (
                "Membership is empty because MPL's official site publishes no careers page or job "
                "listing on its own journey, not because an enumerated listing was empty. The "
                "parent-group M-League Darwinbox board and LinkedIn aggregator were excluded."
            ),
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The ledger URL is MPL's own WordPress 404 page (title '404 Not Found | MPL', h1 "
            "'No Results Found'). The official homepage, About and Life at MPL pages, the "
            f"{sitemap_records['page_sitemap']['url_count']}-URL page sitemap, the "
            f"{sitemap_records['post_sitemap']['url_count']}-URL post sitemap and the WordPress "
            f"page/post/navigation indexes publish no careers or jobs route and link/embed no ATS. "
            "The About page names M-League as MPL's parent; the guessed M-League Darwinbox tenant "
            "is parent-wide and unlinked, so it is not used. The empty result is an explicit "
            "no-careers-page outcome, not a successful zero-job listing."
        )
        return self._finish(source_started)
