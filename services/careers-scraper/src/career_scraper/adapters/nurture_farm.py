"""nurture.farm's reviewed official careers journey: no public listing.

Reviewed 2026-11-07 from the company's own domain:

* the ledger URL ``https://nurture.farm/careers/`` is the site's own WordPress
  404 page (no redirect, title ``Page not found – nurture.farm``, h1
  ``The page can’t be found.``, ``noindex,nofollow``);
* the current site (17 published WordPress pages, rebuilt in 2026) publishes no
  careers/jobs/join-us route, no listing request, no job postings, no ATS link
  or embed and no ``JobPosting`` JSON-LD;
* the last published careers page, ``/join-us-2/`` (Wayback capture
  ``20260508075337``), is a culture page whose only listing action was
  ``View Opportunities`` → ``https://nurture.skillate.com/`` (the company's own
  Skillate tenant).  Today ``/join-us-2/`` is also 404 and the Skillate app host
  accepts no TCP connection at all, so the board cannot be enumerated.

This adapter therefore re-verifies the absence (and the removed careers route),
probes the officially linked legacy ATS, records an explicit
``no_public_careers_page`` / ``legacy_ats_unreachable`` failure and never creates
a job, a count or a date.  If any reviewed page starts publishing a listing, or
the legacy ATS becomes reachable, it stops with
``unreviewed_careers_journey_detected`` so the new journey is inspected before
any membership is collected.
"""
from __future__ import annotations

from hashlib import sha256
import gzip
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


OFFICIAL_ENTRY = "https://nurture.farm/careers/"
OFFICIAL_SITE = "https://nurture.farm/"
ABOUT_PAGE = "https://nurture.farm/about-us/"
LAST_PUBLISHED_CAREERS = "https://nurture.farm/join-us-2/"
HISTORICAL_CAREERS_2022 = "https://nurture.farm/career/"
LEGACY_ATS = "https://nurture.skillate.com/"
ROBOTS = "https://nurture.farm/robots.txt"
WP_SITEMAP = "https://nurture.farm/wp-sitemap.xml"
WP_PAGES = ("https://nurture.farm/wp-json/wp/v2/pages?per_page=100&_fields="
            "id,date,modified,slug,link,title,status,type,parent")
WP_NAVIGATION = "https://nurture.farm/wp-json/wp/v2/navigation?per_page=100"
WP_SEARCH = {
    query: f"https://nurture.farm/wp-json/wp/v2/search?search={query}&per_page=20"
    for query in ("job", "hiring", "vacancy", "career")
}

OFFICIAL_HOSTS = {"nurture.farm", "www.nurture.farm"}
# h1 is normalized (curly apostrophe -> straight) before comparison.
EXPECTED_404_TITLE = "Page not found – nurture.farm"
EXPECTED_404_H1 = "The page can't be found."
EXPECTED_HOME_TITLE = "nurture.farm"
EXPECTED_ABOUT_TITLE = "About Us – nurture.farm"

# A listing route published by the site itself: careers/jobs/join-us style paths.
CAREER_PATH = re.compile(
    r"/(?:careers?|jobs?|join[-_]?us(?:[-_]?\d+)?|open[-_]?roles?|"
    r"vacanc(?:y|ies)|work[-_]with[-_]us|positions?|apply)(?:/|$)",
    re.I,
)
CAREER_WORD = re.compile(
    r"\b(?:careers?|jobs?|hiring|vacanc(?:y|ies)|join[-_]?us|open[-_]?roles?)\b", re.I)
# Third-party applicant-tracking systems.  A link here is a listing signal that
# must be reviewed; it is not collected automatically.
ATS_HOSTS = (
    "skillate.com", "greenhouse.io", "lever.co", "myworkdayjobs.com",
    "workdayjobs.com", "workday.com", "workable.com", "smartrecruiters.com",
    "ashbyhq.com", "keka.com", "darwinbox.in", "zohorecruit.com",
    "zohorecruit.in", "successfactors.com", "sapsf.com", "icims.com",
    "taleo.net", "jobvite.com", "recruitee.com", "breezy.hr", "personio.de",
    "teamtailor.com", "jobscore.com", "recruiterbox.com", "applytojob.com",
    "bamboohr.com", "rippling.com", "eightfold.ai", "phenompeople.com",
    "avature.net", "oraclecloud.com", "turbohire.co", "hackerearth.com",
    "hirect.in", "instahyre.com", "cutshort.io", "wellfound.com",
)
AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.com", "naukri.com", "glassdoor.com",
    "glassdoor.co.in", "monster.com", "foundit.in", "shine.com",
    "timesjobs.com", "hirist.com", "builtin.com", "ziprecruiter.com",
)
AGGREGATOR_JOB_PATH = re.compile(r"/(?:jobs?|job-search|careers?)(?:/|$)", re.I)

ARCHIVE_EVIDENCE = {
    "last_published_careers_route": {
        "url": LAST_PUBLISHED_CAREERS,
        "wayback_capture": "20260508075337",
        "wayback_url": "https://web.archive.org/web/20260508075337id_/https://nurture.farm/join-us-2/",
        "local_fixture": "companies/nurture-farm/source/archive/2026-05-08-join-us-2.html",
        "published_listing_action": LEGACY_ATS,
    },
    "earlier_careers_route": {
        "url": HISTORICAL_CAREERS_2022,
        "wayback_capture": "20220630074941",
        "wayback_url": "https://web.archive.org/web/20220630074941id_/https://nurture.farm/career/",
        "local_fixture": "companies/nurture-farm/source/archive/2022-06-30-career.html",
        "published_listing_action": LEGACY_ATS,
    },
    "careers_path_never_captured": "https://nurture.farm/careers",
    "note": ("Wayback CDX has no capture of /careers/ (the ledger path); the company's "
             "published careers routes were /join-us/, /career/ and /join-us-2/, all of which "
             "linked only to https://nurture.skillate.com/."),
}


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _visible_text(soup: BeautifulSoup) -> str:
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def _without_fragment(url: str) -> str:
    return urlsplit(url)._replace(fragment="").geturl()


def _anchor_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    links, seen = [], set()
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(base_url, href)
        if not target.startswith(("http://", "https://")):
            continue
        key = (anchor.get_text(" ", strip=True), target)
        if key in seen:
            continue
        seen.add(key)
        links.append({"text": anchor.get_text(" ", strip=True), "href": href, "url": target})
    return links


def _career_like(url: str) -> bool:
    parts = urlsplit(url)
    if parts.netloc and parts.netloc.lower() not in OFFICIAL_HOSTS:
        return False
    return bool(CAREER_PATH.search(parts.path))


def _ats_host(host: str) -> bool:
    host = host.lower()
    return any(host == item or host.endswith("." + item) for item in ATS_HOSTS)


def _aggregator_host(host: str) -> bool:
    host = host.lower()
    return any(host == item or host.endswith("." + item) for item in AGGREGATOR_HOSTS)


def classify_links(links: list[dict], base_url: str | None = None) -> dict:
    """Split links into the listing signals this review cares about."""
    base = _without_fragment(base_url) if base_url else None
    listing_candidates, external_ats, aggregator_jobs, other_external = [], [], [], []
    for link in links:
        if base and _without_fragment(link["url"]) == base:
            continue  # a same-page fragment (e.g. the skip link) is not a route
        host = _host(link["url"])
        if not host or host in OFFICIAL_HOSTS:
            if _career_like(link["url"]):
                listing_candidates.append(link)
            continue
        if _ats_host(host):
            external_ats.append(link)
        elif _aggregator_host(host) and AGGREGATOR_JOB_PATH.search(urlsplit(link["url"]).path):
            aggregator_jobs.append(link)
        else:
            other_external.append(link)
    return {
        "listing_candidates": listing_candidates,
        "external_ats_links": external_ats,
        "aggregator_job_links": aggregator_jobs,
        "other_external_links": other_external,
    }


def _ats_embeds(soup: BeautifulSoup, base_url: str) -> list[str]:
    found = []
    for node in soup.find_all(["iframe", "frame", "script"]):
        src = (node.get("src") or node.get("data-src") or "").strip()
        if not src:
            continue
        target = urljoin(base_url, src)
        if _ats_host(_host(target)):
            found.append(target)
    return sorted(set(found))


def _job_posting_schema(soup: BeautifulSoup) -> int:
    def contains_type(value) -> bool:
        if isinstance(value, dict):
            if value.get("@type") == "JobPosting":
                return True
            return any(contains_type(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_type(item) for item in value)
        return False

    count = 0
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or node.get_text() or "")
        except (TypeError, ValueError):
            continue
        if contains_type(payload):
            count += 1
    return count


def parse_document(html: str, response_url: str, *, expected_hosts=OFFICIAL_HOSTS) -> dict:
    """Parse one official HTML page into the reviewed evidence fields."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc.lower() not in expected_hosts:
        raise ValueError(f"nurture.farm page came from an unexpected host: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text() if soup.title else "")
    h1 = [normalize_text(node.get_text(" ", strip=True)) for node in soup.find_all("h1")]
    canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical = canonical_node.get("href") if canonical_node else None
    robots_meta = soup.find("meta", attrs={"name": "robots"})
    links = _anchor_links(soup, response_url)
    classified = classify_links(links, response_url)
    # JSON-LD scripts are removed from the soup by _visible_text; read them first.
    schema_count = _job_posting_schema(soup)
    embeds = _ats_embeds(soup, response_url)
    headings = [normalize_text(node.get_text(" ", strip=True))
                for node in soup.find_all(["h1", "h2", "h3"])]
    visible = _visible_text(soup)
    return {
        "response_url": response_url,
        "status_host": parts.netloc.lower(),
        "title": title,
        "h1": h1,
        "headings": headings,
        "canonical": canonical,
        "meta_robots": robots_meta.get("content") if robots_meta else None,
        "visible_text": visible,
        "visible_text_chars": len(visible),
        "normalized_text_chars": len(normalize_text(visible)),
        "normalized_text_sha256": sha256(normalize_text(visible).encode()).hexdigest(),
        "anchor_count": len(links),
        "anchors": links,
        "internal_paths": sorted({_path(link["url"]) for link in links
                                  if _host(link["url"]) in OFFICIAL_HOSTS}),
        "listing_candidates": classified["listing_candidates"],
        "external_ats_links": classified["external_ats_links"],
        "aggregator_job_links": classified["aggregator_job_links"],
        "ats_embeds": embeds,
        "job_posting_schema_count": schema_count,
        "html_sha256": sha256((html or "").encode()).hexdigest(),
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(parsed.get("listing_candidates") or parsed.get("external_ats_links")
                or parsed.get("aggregator_job_links") or parsed.get("ats_embeds")
                or parsed.get("job_posting_schema_count"))


def parse_entry_response(*, status_code: int, final_url: str, html: str) -> dict:
    parsed = parse_document(html, final_url)
    if _path(final_url) != "/careers":
        raise ValueError(f"nurture.farm careers entry is not the reviewed /careers path: {final_url}")
    if status_code == 404:
        if parsed["title"] != EXPECTED_404_TITLE:
            raise ValueError(f"nurture.farm 404 title changed: {parsed['title']!r}")
        if parsed["h1"] != [EXPECTED_404_H1]:
            raise ValueError(f"nurture.farm 404 heading changed: {parsed['h1']!r}")
        parsed["kind"] = "official_404"
    elif status_code == 200:
        parsed["kind"] = "published_page"
    else:
        raise ValueError(f"nurture.farm careers entry returned an unreviewed status: HTTP {status_code}")
    parsed["status_code"] = status_code
    return parsed


def parse_careers_route_response(*, status_code: int, final_url: str, html: str) -> dict:
    """The removed /join-us-2/ page: 404 today, a culture page in the 2026 archive."""
    parsed = parse_document(html, final_url)
    if _path(final_url) != "/join-us-2":
        raise ValueError(f"unexpected last-published careers route: {final_url}")
    if status_code == 404:
        if parsed["title"] != EXPECTED_404_TITLE or parsed["h1"] != [EXPECTED_404_H1]:
            raise ValueError("nurture.farm /join-us-2/ 404 identity changed")
        parsed["kind"] = "removed_careers_page"
    elif status_code == 200:
        parsed["kind"] = "published_page"
    else:
        raise ValueError(f"nurture.farm /join-us-2/ returned an unreviewed status: HTTP {status_code}")
    parsed["status_code"] = status_code
    return parsed


def parse_homepage(html: str, response_url: str = OFFICIAL_SITE) -> dict:
    parsed = parse_document(html, response_url)
    if _path(response_url) != "/":
        raise ValueError(f"nurture.farm homepage is not the reviewed root path: {response_url}")
    if parsed["title"] != EXPECTED_HOME_TITLE:
        raise ValueError(f"nurture.farm homepage title changed: {parsed['title']!r}")
    expected_canonical = "https://nurture.farm/"
    if parsed["canonical"] != expected_canonical:
        raise ValueError(f"nurture.farm homepage canonical changed: {parsed['canonical']!r}")
    return parsed


def parse_about_page(html: str, response_url: str = ABOUT_PAGE) -> dict:
    parsed = parse_document(html, response_url)
    if _path(response_url) != "/about-us":
        raise ValueError(f"nurture.farm about page is not the reviewed path: {response_url}")
    if parsed["title"] != EXPECTED_ABOUT_TITLE:
        raise ValueError(f"nurture.farm about page title changed: {parsed['title']!r}")
    if "subsidiary of UPL SAS" not in normalize_text(parsed["visible_text"]):
        raise ValueError("nurture.farm about page lost the reviewed UPL SAS parent statement")
    return parsed


def parse_robots(text: str) -> dict:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("nurture.farm robots.txt is empty")
    sitemaps = []
    for line in lines:
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                sitemaps.append(value)
    career_tokens = [line for line in lines
                     if re.search(r"career|job|hiring|vacanc|recruit|apply", line, re.I)]
    return {
        "lines": lines,
        "sitemaps": sitemaps,
        "career_tokens": career_tokens,
        "disallows_admin": any(line.lower().startswith("disallow:") and "/wp-admin" in line
                               for line in lines),
    }


def parse_sitemap(xml: str) -> dict:
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I)
    if not urls:
        raise ValueError("sitemap contains no <loc> URLs")
    hosts = sorted({_host(url) for url in urls})
    foreign = [url for url in urls if _host(url) not in OFFICIAL_HOSTS]
    career_like = [url for url in urls if _career_like(url)]
    return {"urls": urls, "url_count": len(urls), "hosts": hosts,
            "foreign_host_urls": foreign, "career_like_urls": career_like}


def parse_wp_pages(payload) -> dict:
    if not isinstance(payload, list) or not payload:
        raise ValueError("WordPress page index is not a non-empty list")
    items = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("link"):
            raise ValueError("WordPress page index item is missing its link")
        title = row.get("title")
        title_text = title.get("rendered") if isinstance(title, dict) else title
        items.append({
            "id": row.get("id"),
            "slug": row.get("slug"),
            "link": row["link"],
            "title": normalize_text(title_text),
            "status": row.get("status"),
            "type": row.get("type"),
            "modified": row.get("modified"),
        })
    career_like_items = [item for item in items
                         if _career_like(item["link"])
                         or CAREER_WORD.search(f"{item['slug'] or ''} {item['title']}")]
    foreign_host_links = [item["link"] for item in items
                          if _host(item["link"]) not in OFFICIAL_HOSTS]
    return {"count": len(items), "items": items, "career_like_items": career_like_items,
            "foreign_host_links": foreign_host_links}


def parse_navigation(payload) -> dict:
    if not isinstance(payload, list) or not payload:
        raise ValueError("WordPress navigation index is not a non-empty list")
    links = []
    for row in payload:
        content = row.get("content") if isinstance(row, dict) else None
        html = content.get("rendered") if isinstance(content, dict) else None
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        links.extend(_anchor_links(soup, OFFICIAL_SITE))
    if not links:
        raise ValueError("WordPress navigation publishes no links")
    classified = classify_links(links, OFFICIAL_SITE)
    return {
        "link_count": len(links),
        "links": links,
        "career_like_links": classified["listing_candidates"],
        "external_ats_links": classified["external_ats_links"],
        "aggregator_job_links": classified["aggregator_job_links"],
    }


def parse_wp_search(payload, query: str) -> dict:
    if not isinstance(payload, list):
        raise ValueError(f"WordPress search '{query}' payload is not a list")
    items = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("url"):
            raise ValueError(f"WordPress search '{query}' item is missing its url")
        title = row.get("title")
        items.append({"id": row.get("id"), "url": row["url"], "type": row.get("type"),
                      "title": normalize_text(title if isinstance(title, str) else "")})
    job_like = [item for item in items
                if _career_like(item["url"])
                or _ats_host(_host(item["url"]))
                or (_aggregator_host(_host(item["url"]))
                    and AGGREGATOR_JOB_PATH.search(urlsplit(item["url"]).path))]
    return {"query": query, "count": len(items), "items": items, "job_like_items": job_like}


class NurtureFarm(Adapter):
    def _phase(self, name: str, started: float):
        self.result.timings[name] = round(max(0.0, time.perf_counter() - started), 6)

    def _request_log_lines(self) -> int:
        """Number of response records before this request (4xx bodies are archived)."""
        output = getattr(self.http, "output", None)
        log = output / "requests.jsonl" if output else None
        if not log or not log.exists():
            return 0
        with log.open("rb") as handle:
            return sum(1 for _ in handle)

    def _capture_since(self, offset: int, url: str) -> dict | None:
        """The shared HTTP client archives a 4xx body before raising; read it back."""
        output = getattr(self.http, "output", None)
        log = output / "requests.jsonl" if output else None
        if not log or not log.exists():
            return None
        with log.open("r") as handle:
            lines = handle.readlines()[offset:]
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if url in (record.get("url"), record.get("final_url")):
                return record
        return None

    async def _fetch(self, url: str, purpose: str, **kwargs):
        started = time.perf_counter()
        record = {"url": url, "purpose": purpose}
        offset = self._request_log_lines()
        try:
            response = await self.http.get(url, **kwargs)
            record.update({"outcome": "received", "status_code": response.status_code,
                           "final_url": str(response.url), "body": response.content,
                           "html_sha256": sha256(response.content).hexdigest()})
            return response, record
        except Exception as exc:  # noqa: BLE001 - evidence, not control flow
            capture = self._capture_since(offset, url)
            body = None
            if capture and capture.get("sha256"):
                archived = getattr(self.http, "output", None)
                path = archived / "responses" / f"{capture['sha256']}.gz" if archived else None
                if path and path.exists():
                    body = gzip.decompress(path.read_bytes())
            if capture and body is not None:
                # The archived HTTP error status is evidence (it is the reviewed 404);
                # the client exception is kept separately so ``error`` means "no response".
                record.update({"outcome": "http_error",
                               "status_code": capture.get("status"),
                               "final_url": capture.get("final_url"),
                               "body": body, "html_sha256": capture.get("sha256"),
                               "redirects": capture.get("redirects"),
                               "client_error": f"{type(exc).__name__}: {exc}"})
            else:
                record.update({"outcome": "request_failed",
                               "error": f"{type(exc).__name__}: {exc}"})
            return None, record
        finally:
            record["duration_seconds"] = max(0.0, time.perf_counter() - started)

    @staticmethod
    def _evidence(record: dict) -> dict:
        return {"requested_url": record["url"], "purpose": record["purpose"],
                "outcome": record.get("outcome"), "status_code": record.get("status_code"),
                "final_url": record.get("final_url"), "error": record.get("error"),
                "client_error": record.get("client_error"),
                "html_sha256": record.get("html_sha256"),
                "duration_seconds": round(record.get("duration_seconds", 0.0), 6)}

    def _stop(self, error: str, conclusion: str):
        self.result.errors.append(error)
        self.result.metadata["observed_journey"]["conclusion"] = conclusion
        return self._finish()

    def _parse(self, record: dict, parser, *, what: str):
        body = record.get("body")
        if body is None:
            raise ValueError(f"{what} response body is unavailable: {record.get('error')}")
        return parser(body.decode("utf-8", errors="replace"))

    def _finish(self):
        if getattr(self, "_source_started", None) is not None:
            self.result.timings["source_collection_seconds"] = round(
                max(0.0, time.perf_counter() - self._source_started), 6)
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
        self._source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": "nurture-farm.official-no-careers-page.1",
            "official_entry_url": OFFICIAL_ENTRY,
            "official_site_url": OFFICIAL_SITE,
            "about_page_url": ABOUT_PAGE,
            "last_published_careers_route_url": LAST_PUBLISHED_CAREERS,
            "legacy_ats_url": LEGACY_ATS,
            "legacy_ats_provenance": ARCHIVE_EVIDENCE,
            "listing_authority": "none: the official site publishes no careers page or job listing",
            "detail_method": "none: no nurture.farm role, requisition or detail route is published",
            "request_scope": (
                "Only nurture.farm's own ledger URL, homepage, About page, removed /join-us-2/ "
                "careers route, robots.txt, WordPress sitemap, WordPress page/navigation/search "
                "REST indexes, and the officially linked legacy ATS host. No guessed ATS tenant, "
                "parent UPL board or aggregator is queried for membership."
            ),
            "observed_journey": {},
        })

        # 1. Ledger URL: must still be the reviewed WordPress 404 with no listing signal.
        started = time.perf_counter()
        _, entry_record = await self._fetch(OFFICIAL_ENTRY, "official_careers_entry")
        try:
            entry = self._parse(
                entry_record,
                lambda html: parse_entry_response(
                    status_code=entry_record["status_code"],
                    final_url=entry_record.get("final_url") or "",
                    html=html),
                what="official careers entry")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["initial_entry"] = self._evidence(entry_record)
            self.result.errors.append(f"nurture_farm_official_entry_failed: {type(exc).__name__}: {exc}")
            self._phase("entry_check_seconds", started)
            return self._finish()
        self._phase("entry_check_seconds", started)
        self.result.metadata["initial_entry"] = {
            **self._evidence(entry_record), "kind": entry["kind"], "title": entry["title"],
            "h1": entry["h1"], "canonical": entry["canonical"],
            "meta_robots": entry["meta_robots"], "anchor_count": entry["anchor_count"],
            "listing_candidates": entry["listing_candidates"],
            "external_ats_links": entry["external_ats_links"],
            "ats_embeds": entry["ats_embeds"],
            "job_posting_schema_count": entry["job_posting_schema_count"],
            "visible_text_chars": entry["visible_text_chars"],
            "normalized_text_chars": entry["normalized_text_chars"],
            "normalized_text_sha256": entry["normalized_text_sha256"],
        }
        self.result.metadata["observed_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "final_url": entry["response_url"],
            "status_code": entry["status_code"],
            "title": entry["title"],
            "h1": entry["h1"],
            "canonical": entry["canonical"],
            "headings": entry["headings"],
            "anchor_count": entry["anchor_count"],
            "listing_candidates": entry["listing_candidates"],
            "external_ats_links": entry["external_ats_links"],
            "ats_embeds": entry["ats_embeds"],
            "job_posting_schema_count": entry["job_posting_schema_count"],
            "visible_text_chars": entry["visible_text_chars"],
            "normalized_text_sha256": entry["normalized_text_sha256"],
        }
        if entry["kind"] != "official_404":
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The ledger careers URL now serves a published page instead of the reviewed 404; "
                "the new journey must be inspected before any membership is collected.")
        if has_listing_signal(entry):
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The 404 response now carries a job-listing signal; the journey must be inspected "
                "before any membership is collected.")

        # 2. Homepage and About page: navigation/footer must publish no careers route or ATS.
        for url, parser, name, purpose in (
                (OFFICIAL_SITE, parse_homepage, "official_site", "official_homepage"),
                (ABOUT_PAGE, parse_about_page, "about_page", "official_about")):
            started = time.perf_counter()
            _, record = await self._fetch(url, purpose)
            try:
                page = self._parse(record, parser, what=name)
            except Exception as exc:  # noqa: BLE001
                self.result.metadata[name] = self._evidence(record)
                self.result.errors.append(f"nurture_farm_{name}_failed: {type(exc).__name__}: {exc}")
                self._phase(f"{name}_seconds", started)
                return self._finish()
            self._phase(f"{name}_seconds", started)
            self.result.metadata[name] = {
                **self._evidence(record), "title": page["title"],
                "canonical": page["canonical"], "anchor_count": page["anchor_count"],
                "internal_paths": page["internal_paths"],
                "listing_candidates": page["listing_candidates"],
                "external_ats_links": page["external_ats_links"],
                "aggregator_job_links": page["aggregator_job_links"],
                "ats_embeds": page["ats_embeds"],
                "job_posting_schema_count": page["job_posting_schema_count"],
                "visible_text_chars": page["visible_text_chars"],
                "normalized_text_chars": page["normalized_text_chars"],
                "normalized_text_sha256": page["normalized_text_sha256"],
            }
            if has_listing_signal(page):
                return self._stop(
                    "unreviewed_careers_journey_detected",
                    f"The {name.replace('_', ' ')} now links or embeds a careers/job listing; the "
                    "new journey must be inspected before any membership is collected.")
            if any(_career_like(path) for path in page["internal_paths"]):
                return self._stop(
                    "unreviewed_careers_journey_detected",
                    f"The {name.replace('_', ' ')} now links a careers/jobs path; the new journey "
                    "must be inspected before any membership is collected.")

        # 3. Last published careers route: 404 today, culture page in the 2026 archive.
        started = time.perf_counter()
        _, join_record = await self._fetch(LAST_PUBLISHED_CAREERS, "last_published_careers_route")
        try:
            join = self._parse(
                join_record,
                lambda html: parse_careers_route_response(
                    status_code=join_record["status_code"],
                    final_url=join_record.get("final_url") or "",
                    html=html),
                what="last published careers route")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["last_published_careers_route"] = self._evidence(join_record)
            self.result.errors.append(
                f"nurture_farm_last_careers_route_failed: {type(exc).__name__}: {exc}")
            self._phase("last_careers_route_seconds", started)
            return self._finish()
        self._phase("last_careers_route_seconds", started)
        self.result.metadata["last_published_careers_route"] = {
            **self._evidence(join_record), "kind": join["kind"], "title": join["title"],
            "h1": join["h1"], "listing_candidates": join["listing_candidates"],
            "external_ats_links": join["external_ats_links"],
            "ats_embeds": join["ats_embeds"],
            "job_posting_schema_count": join["job_posting_schema_count"],
            "normalized_text_sha256": join["normalized_text_sha256"],
        }
        if join["kind"] != "removed_careers_page":
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The last published careers route /join-us-2/ now serves a published page; the new "
                "journey must be inspected before any membership is collected.")
        if has_listing_signal(join):
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The /join-us-2/ 404 now carries a listing signal; the journey must be inspected "
                "before any membership is collected.")

        # 4. robots.txt and the WordPress sitemap (both must publish no careers route).
        started = time.perf_counter()
        _, robots_record = await self._fetch(ROBOTS, "robots")
        try:
            robots = self._parse(robots_record, parse_robots, what="robots.txt")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["robots"] = self._evidence(robots_record)
            self.result.errors.append(f"nurture_farm_robots_failed: {type(exc).__name__}: {exc}")
            self._phase("robots_seconds", started)
            return self._finish()
        self._phase("robots_seconds", started)
        self.result.metadata["robots"] = {**self._evidence(robots_record),
                                          "sitemaps": robots["sitemaps"],
                                          "career_tokens": robots["career_tokens"]}
        if robots["career_tokens"]:
            return self._stop(
                "unreviewed_careers_journey_detected",
                "robots.txt now names a careers/jobs route; the new journey must be inspected "
                "before any membership is collected.")
        for sitemap_url in robots["sitemaps"]:
            if _host(sitemap_url) not in OFFICIAL_HOSTS:
                self.result.metadata.setdefault("foreign_sitemaps", []).append(sitemap_url)
                continue
            _, sitemap_record = await self._fetch(sitemap_url, "robots_declared_sitemap")
            try:
                sitemap = self._parse(sitemap_record, parse_sitemap, what="robots sitemap")
            except Exception as exc:  # noqa: BLE001
                self.result.metadata.setdefault("robots_sitemaps", []).append(
                    self._evidence(sitemap_record))
                self.result.errors.append(
                    f"nurture_farm_robots_sitemap_failed: {type(exc).__name__}: {exc}")
                return self._finish()
            self.result.metadata.setdefault("robots_sitemaps", []).append(
                {**self._evidence(sitemap_record), "url_count": sitemap["url_count"],
                 "career_like_urls": sitemap["career_like_urls"]})
            if sitemap["career_like_urls"]:
                return self._stop(
                    "unreviewed_careers_journey_detected",
                    "A robots-declared sitemap now publishes a careers/job URL; the new journey "
                    "must be inspected before any membership is collected.")

        started = time.perf_counter()
        _, wp_sitemap_record = await self._fetch(WP_SITEMAP, "wordpress_sitemap")
        if wp_sitemap_record.get("status_code") == 200:
            try:
                wp_sitemap = self._parse(wp_sitemap_record, parse_sitemap, what="WordPress sitemap")
            except Exception as exc:  # noqa: BLE001
                self.result.metadata["wp_sitemap"] = self._evidence(wp_sitemap_record)
                self.result.errors.append(f"nurture_farm_wp_sitemap_failed: {type(exc).__name__}: {exc}")
                self._phase("wp_sitemap_seconds", started)
                return self._finish()
            self.result.metadata["wp_sitemap"] = {
                **self._evidence(wp_sitemap_record), "url_count": wp_sitemap["url_count"],
                "career_like_urls": wp_sitemap["career_like_urls"]}
            if wp_sitemap["career_like_urls"]:
                self._phase("wp_sitemap_seconds", started)
                return self._stop(
                    "unreviewed_careers_journey_detected",
                    "The WordPress sitemap now publishes a careers/job URL; the new journey must "
                    "be inspected before any membership is collected.")
        else:
            self.result.metadata["wp_sitemap"] = {
                **self._evidence(wp_sitemap_record),
                "note": ("WordPress sitemaps are disabled (the reviewed 404 body); the WordPress "
                         "page index below is the published-page authority.")}
        self._phase("wp_sitemap_seconds", started)

        # 5. WordPress page index, navigation and search: no careers page/post/type.
        started = time.perf_counter()
        _, pages_record = await self._fetch(WP_PAGES, "wp_pages")
        try:
            pages = self._parse(pages_record, lambda body: parse_wp_pages(json.loads(body)),
                                what="WordPress page index")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["wp_pages"] = self._evidence(pages_record)
            self.result.errors.append(f"nurture_farm_wp_pages_failed: {type(exc).__name__}: {exc}")
            self._phase("wp_pages_seconds", started)
            return self._finish()
        self._phase("wp_pages_seconds", started)
        self.result.metadata["wp_pages"] = {
            **self._evidence(pages_record), "count": pages["count"], "items": pages["items"],
            "career_like_items": pages["career_like_items"],
            "foreign_host_links": pages["foreign_host_links"]}
        if pages["career_like_items"] or pages["foreign_host_links"]:
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The WordPress page index now publishes a careers/job or foreign-host page; the "
                "new journey must be inspected before any membership is collected.")

        started = time.perf_counter()
        _, nav_record = await self._fetch(WP_NAVIGATION, "wp_navigation")
        try:
            navigation = self._parse(nav_record, lambda body: parse_navigation(json.loads(body)),
                                     what="WordPress navigation")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["wp_navigation"] = self._evidence(nav_record)
            self.result.errors.append(f"nurture_farm_wp_navigation_failed: {type(exc).__name__}: {exc}")
            self._phase("wp_navigation_seconds", started)
            return self._finish()
        self._phase("wp_navigation_seconds", started)
        self.result.metadata["wp_navigation"] = {
            **self._evidence(nav_record), "link_count": navigation["link_count"],
            "links": navigation["links"], "career_like_links": navigation["career_like_links"],
            "external_ats_links": navigation["external_ats_links"],
            "aggregator_job_links": navigation["aggregator_job_links"]}
        if navigation["career_like_links"] or navigation["external_ats_links"] \
                or navigation["aggregator_job_links"]:
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The official WordPress navigation now publishes a careers/job or ATS link; the "
                "new journey must be inspected before any membership is collected.")

        started = time.perf_counter()
        searches = {}
        for query, url in WP_SEARCH.items():
            _, search_record = await self._fetch(url, f"wp_search_{query}")
            try:
                payload = json.loads(search_record.get("body") or b"null")
                searches[query] = {
                    **self._evidence(search_record),
                    **parse_wp_search(payload, query)}
            except Exception as exc:  # noqa: BLE001
                self.result.metadata.setdefault("wp_search", {})[query] = self._evidence(search_record)
                self.result.errors.append(
                    f"nurture_farm_wp_search_{query}_failed: {type(exc).__name__}: {exc}")
                self._phase("wp_search_seconds", started)
                return self._finish()
            if searches[query]["job_like_items"]:
                self.result.metadata["wp_search"] = searches
                self._phase("wp_search_seconds", started)
                return self._stop(
                    "unreviewed_careers_journey_detected",
                    f"The WordPress search '{query}' now returns a job-listing item; the new "
                    "journey must be inspected before any membership is collected.")
        self._phase("wp_search_seconds", started)
        self.result.metadata["wp_search"] = searches

        # 6. The officially linked legacy ATS.  It is unreachable today; if it ever
        #    answers, its membership must be reviewed before anything is collected.
        started = time.perf_counter()
        ats_response, ats_record = await self._fetch(
            LEGACY_ATS, "legacy_ats_officially_linked", timeout=15.0)
        self._phase("legacy_ats_probe_seconds", started)
        self.result.metadata["legacy_ats"] = {
            **self._evidence(ats_record),
            "published_by": "the last published careers page /join-us-2/ (Wayback 20260508075337)",
        }
        if ats_response is not None:
            self.result.metadata["legacy_ats"]["reachable"] = True
            self.result.errors.append("legacy_ats_reachable")
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The officially linked Skillate board now answers; its membership, pagination and "
                "detail routes must be inspected before any collection.")
        self.result.metadata["legacy_ats"]["reachable"] = False
        self.result.metadata["legacy_ats"]["server_answered"] = (
            ats_record.get("status_code") is not None)
        self.result.errors.append("legacy_ats_unreachable")

        # 7. Final recheck of the same ledger URL.
        started = time.perf_counter()
        _, recheck_record = await self._fetch(OFFICIAL_ENTRY, "official_entry_final_recheck")
        try:
            recheck = self._parse(
                recheck_record,
                lambda html: parse_entry_response(
                    status_code=recheck_record["status_code"],
                    final_url=recheck_record.get("final_url") or "",
                    html=html),
                what="official careers entry recheck")
        except Exception as exc:  # noqa: BLE001
            self.result.metadata["final_recheck"] = self._evidence(recheck_record)
            self.result.errors.append(f"nurture_farm_final_recheck_failed: {type(exc).__name__}: {exc}")
            self._phase("final_recheck_seconds", started)
            return self._finish()
        self._phase("final_recheck_seconds", started)
        self.result.metadata["final_recheck"] = {
            **self._evidence(recheck_record), "kind": recheck["kind"], "title": recheck["title"],
            "h1": recheck["h1"], "normalized_text_sha256": recheck["normalized_text_sha256"]}
        entry_identity = (entry["status_code"], entry["title"], entry["h1"],
                          entry["normalized_text_sha256"], has_listing_signal(entry))
        recheck_identity = (recheck["status_code"], recheck["title"], recheck["h1"],
                            recheck["normalized_text_sha256"], has_listing_signal(recheck))
        matches = entry_identity == recheck_identity
        self.result.metadata["final_recheck_matches_initial"] = matches
        self.result.metadata["final_recheck_html_sha256_matches"] = (
            entry["html_sha256"] == recheck["html_sha256"])
        if not matches:
            self.result.errors.append("nurture_farm_ledger_entry_changed_between_requests")
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The ledger URL changed between the initial and final requests; the journey must "
                "be inspected before any membership is collected.")
        if recheck["kind"] != "official_404" or has_listing_signal(recheck):
            return self._stop(
                "unreviewed_careers_journey_detected",
                "The ledger URL changed between the initial and final requests; the journey must "
                "be inspected before any membership is collected.")

        if "legacy_ats_unreachable" in self.result.errors:
            self.result.errors.insert(0, "no_public_careers_page")
        else:
            self.result.errors.append("no_public_careers_page")

        self.result.metadata["membership"] = {
            "advertised_jobs": None,
            "listing_rows": 0,
            "careers_page_http_status": entry["status_code"],
            "careers_page_title": entry["title"],
            "careers_page_h1": entry["h1"],
            "careers_route_published": False,
            "careers_path_ever_captured": False,
            "wp_pages": pages["count"],
            "wp_navigation_links": navigation["link_count"],
            "career_pages_published": 0,
            "job_listing_candidates": [],
            "external_ats_links": [],
            "ats_embeds": [],
            "job_posting_schema_count": 0,
            "last_published_careers_route": LAST_PUBLISHED_CAREERS,
            "last_published_careers_route_status": join["status_code"],
            "legacy_ats_url": LEGACY_ATS,
            "legacy_ats_reachable": False,
            "reconciled": True,
            "note": (
                "Membership is empty because the official site publishes no careers page or job "
                "listing, and the company's own Skillate tenant (its only published listing "
                "destination) is unreachable. This is an explicit failure, not a zero-vacancy claim."),
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The ledger URL is the site's own WordPress 404 (title 'Page not found – nurture.farm', "
            "h1 'The page can’t be found.'). The current site publishes "
            f"{pages['count']} WordPress pages, no careers/jobs/join-us route, no listing request, "
            "no ATS link or embed and no JobPosting JSON-LD. The last published careers route "
            "/join-us-2/ is 404 today; its 2026-05-08 capture linked only to "
            "https://nurture.skillate.com/, whose app host accepts no TCP connection. Nothing is "
            "collected and advertised_jobs stays null.")
        return self._finish()
