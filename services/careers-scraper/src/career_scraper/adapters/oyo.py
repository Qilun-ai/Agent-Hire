"""OYO's official careers journey, reviewed 2026-11-08.

The ledger entry ``https://www.oyorooms.com/careers/`` is no longer a careers
page: it permanently redirects (301) to ``http://www.oyorooms.com/`` and then to
the OYO hotel homepage ``https://www.oyorooms.com/``.  The homepage footer and
the official About page publish exactly one career destination,
``https://www.linkedin.com/company/oyo-rooms/jobs/``, which is an aggregator and
is explicitly out of scope for company-scoped membership.

There is no OYO-owned careers listing, job card, job path, ATS iframe/link or
JobPosting JSON-LD on the official pages.  This adapter therefore records the
removed route and the no-listing outcome explicitly.  It never turns the
redirect into a zero-job success and never substitutes a guessed ATS slug, the
LinkedIn board or a parent-company board.  If OYO restores a real careers page
or publishes a job/ATS signal, the adapter stops with
``unreviewed_careers_listing_detected`` (or ``official_careers_route_changed``)
for a fresh human inspection instead of scraping an unreviewed membership.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.oyorooms.com/careers/"
OFFICIAL_HOME = "https://www.oyorooms.com/"
OFFICIAL_ABOUT = "https://www.oyorooms.com/about/"
LINKEDIN_CAREERS = "https://www.linkedin.com/company/oyo-rooms/jobs/"
EXPECTED_TITLE = "OYO: Save Up to 70% on Hotel Booking | Free Cancellation & Pay at Hotel"
EXPECTED_H1 = "Top rated value stays across 35+ countries"
EXPECTED_ABOUT_TITLE = "OYO | About US"
EXPECTED_ABOUT_PHRASE = "Join our team"
PUBLIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
EXPECTED_REDIRECTS = (
    (301, OFFICIAL_ENTRY, "http://www.oyorooms.com/"),
    (301, "http://www.oyorooms.com/", "https://www.oyorooms.com/"),
)

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
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"linkedin\.com|indeed\.com|naukri\.com|glassdoor\.com|monster\.com|"
    r"ziprecruiter\.com|wellfound\.com|angel\.co|instahyre\.com|hirist\.com|"
    r"cutshort\.io|foundit\.in|timesjobs\.com|shine\.com|simplyhired\.com|"
    r"jobstreet\.com|seek\.com|bayt\.com|efinancialcareers\.com"
    r")$"
)
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:job|jobs|job-|position|positions|opening|openings|"
    r"vacanc|vacancy|apply)(?:/|$|[?#])"
)
CAREER_ROLE_PATH = re.compile(r"^/careers/[a-z0-9][a-z0-9._-]{2,}/?$", re.I)
CAREER_NAV_PATH = re.compile(r"^/careers?/?$", re.I)
CAREER_KEYWORD = re.compile(r"(?i)\b(?:career|careers|job|jobs|hiring|talent|recruit|join our team|work with us)\b")
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b")
AGGREGATOR_TEXT = re.compile(r"(?i)^(?:teams?\s*/\s*careers|careers?|jobs?|join our team|open roles|view jobs)$")


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


def _same_url(first: str, second: str) -> bool:
    one, two = urlsplit(first), urlsplit(second)
    return (
        one.scheme.lower() == two.scheme.lower()
        and one.netloc.lower() == two.netloc.lower()
        and (one.path.rstrip("/") or "/") == (two.path.rstrip("/") or "/")
    )


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


def redirect_chain(response) -> list[dict]:
    """Return the followed redirect hop list in the exact order observed."""
    return [
        {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
        for item in response.history
    ]


def validate_entry_journey(response) -> list[dict]:
    """Require the reviewed ``/careers/`` -> ``http://www.oyorooms.com/`` -> home journey.

    A restored careers page (no redirect, or a final ``/careers`` URL) must not
    be parsed as an empty listing; it is a changed journey for human review.
    """
    history = redirect_chain(response)
    final_url = str(response.url)
    if not history:
        raise ValueError("official_careers_route_changed: /careers/ no longer redirects to the homepage")
    if not _same_url(final_url, OFFICIAL_HOME):
        raise ValueError(
            f"official_careers_route_changed: /careers/ ended at {final_url!r}, not the OYO homepage"
        )
    first = history[0]
    if first["status"] != 301 or not _same_url(first["url"], OFFICIAL_ENTRY):
        raise ValueError(f"official_careers_route_changed: unexpected first hop {first!r}")
    if not _same_url(first.get("location") or "", "http://www.oyorooms.com/"):
        raise ValueError(
            "official_careers_route_changed: /careers/ no longer redirects to http://www.oyorooms.com/"
        )
    if len(history) < 2:
        raise ValueError("official_careers_route_changed: missing the HTTP -> HTTPS homepage redirect")
    second = history[1]
    if second["status"] != 301 or not _same_url(second["url"], "http://www.oyorooms.com/"):
        raise ValueError(f"official_careers_route_changed: unexpected second hop {second!r}")
    if not _same_url(second.get("location") or "", OFFICIAL_HOME):
        raise ValueError(
            "official_careers_route_changed: the HTTP homepage no longer redirects to HTTPS"
        )
    return history


def _anchor_record(anchor, response_url: str) -> dict | None:
    href = (anchor.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "tel:", "mailto:")):
        return None
    absolute = urljoin(response_url, href)
    return {
        "text": normalize_text(anchor.get_text(" ", strip=True)),
        "href": href,
        "url": absolute,
        "host": _host(absolute),
        "path": urlsplit(absolute).path,
    }


def _listing_links(anchors: list[dict]) -> dict:
    career_links, aggregator_links, ats_links, candidates, nav_links = [], [], [], [], []
    for anchor in anchors:
        host = anchor["host"]
        path = anchor["path"]
        text = anchor["text"]
        is_ats = bool(ATS_HOST.search(host))
        is_aggregator = bool(AGGREGATOR_HOST.search(host))
        is_job_path = bool(JOB_PATH.search(path))
        is_role_path = bool(CAREER_ROLE_PATH.match(path))
        is_career_nav = bool(CAREER_NAV_PATH.match(path))
        is_career_text = bool(CAREER_KEYWORD.search(text))
        if is_ats:
            ats_links.append(anchor)
        elif is_aggregator:
            aggregator_links.append(anchor)
        elif is_job_path or is_role_path:
            candidates.append(anchor)
        elif is_career_nav:
            nav_links.append(anchor)
        if is_ats or is_aggregator or is_job_path or is_role_path or is_career_nav or is_career_text:
            career_links.append(anchor)
    return {
        "career_links": career_links,
        "aggregator_links": aggregator_links,
        "external_ats_links": ats_links,
        "job_listing_candidates": candidates,
        "internal_career_nav": nav_links,
    }


def _ats_embeds(soup: BeautifulSoup, response_url: str) -> list[dict]:
    embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = urljoin(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    return embeds


def parse_homepage(html: str, response_url: str = OFFICIAL_HOME) -> dict:
    """Parse the OYO homepage reached from the removed careers route.

    Raises ``ValueError`` when the page is not the exact reviewed homepage, so a
    redesign is reported rather than silently treated as an empty careers page.
    """
    parts = urlsplit(response_url)
    if parts.scheme != "https" or _host(response_url) not in {"www.oyorooms.com", "oyorooms.com"}:
        raise ValueError(f"official_careers_route_changed: unexpected final host {response_url!r}")
    if (parts.path.rstrip("/") or "/") != "/":
        raise ValueError(f"official_careers_route_changed: /careers/ ended at path {parts.path!r}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not (title == EXPECTED_TITLE or (title.startswith("OYO") and "Hotel Booking" in title)):
        raise ValueError(f"oyo_homepage_title_changed: {title!r}")
    h1 = [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if not h1:
        raise ValueError("oyo_homepage_missing_h1")

    text = visible_text(html)
    normalized = normalize_text(text)
    anchors = [item for anchor in soup.select("a[href]") if (item := _anchor_record(anchor, response_url))]
    links = _listing_links(anchors)
    embeds = _ats_embeds(soup, response_url)

    pagination_controls = []
    for element in soup.select("a,button"):
        label = normalize_text(element.get_text(" ", strip=True))
        rel = (element.get("rel") or []) if element.name == "a" else []
        if (label and PAGINATION_TEXT.search(label)) or "next" in [value.lower() for value in rel]:
            pagination_controls.append(label or "rel=next")

    schema_count = job_posting_schema_count(soup)
    return {
        "response_url": response_url,
        "title": title,
        "h1": h1,
        "h1_matches_reviewed": EXPECTED_H1 in h1,
        "headings": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "anchor_count": len(anchors),
        "anchors": anchors,
        **links,
        "ats_embeds": embeds,
        "job_posting_schema_count": schema_count,
        "pagination_controls": pagination_controls,
        "visible_text_chars": len(text),
        "normalized_text_chars": len(normalized),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
    }


def parse_about(html: str, response_url: str = OFFICIAL_ABOUT) -> dict:
    """Parse the official About page's only career CTA (LinkedIn)."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or _host(response_url) not in {"www.oyorooms.com", "oyorooms.com"}:
        raise ValueError(f"oyo_about_page_unexpected_url: {response_url!r}")
    if (parts.path.rstrip("/") or "/") != "/about":
        raise ValueError(f"oyo_about_page_unexpected_path: {parts.path!r}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_ABOUT_TITLE:
        raise ValueError(f"oyo_about_title_changed: {title!r}")
    text = visible_text(html)
    if EXPECTED_ABOUT_PHRASE not in normalize_text(text):
        raise ValueError("oyo_about_page_missing_join_our_team")
    anchors = [item for anchor in soup.select("a[href]") if (item := _anchor_record(anchor, response_url))]
    links = _listing_links(anchors)
    return {
        "response_url": response_url,
        "title": title,
        "anchor_count": len(anchors),
        **links,
        "ats_embeds": _ats_embeds(soup, response_url),
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": [],
        "visible_text_chars": len(text),
        "normalized_text_sha256": sha256(normalize_text(text).encode()).hexdigest(),
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["job_listing_candidates"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
    )


def _evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "h1": parsed["h1"],
        "career_link_urls": [item["url"] for item in parsed["career_links"]],
        "aggregator_urls": [item["url"] for item in parsed["aggregator_links"]],
        "internal_career_nav": [item["url"] for item in parsed["internal_career_nav"]],
        "listing_candidate_urls": [item["url"] for item in parsed["job_listing_candidates"]],
        "external_ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
    }


def _entry_evidence(response) -> dict:
    return {
        "requested_url": OFFICIAL_ENTRY,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "html_bytes": len(response.content),
        "html_sha256": sha256(response.content).hexdigest(),
        "redirects": redirect_chain(response),
        "fetched_at": now(),
    }


class Oyo(Adapter):
    """Read the official OYO journey and record its removed careers listing."""

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "official_home_url": OFFICIAL_HOME,
            "official_about_url": OFFICIAL_ABOUT,
            "listing_authority": (
                "none: the official /careers/ route is removed (301 -> homepage), and the only "
                "published career destination is LinkedIn jobs (aggregator), which is out of scope"
            ),
            "detail_method": "none: no company-owned role, listing or detail route is published",
            "request_scope": (
                "Official oyorooms.com careers entry, its homepage redirect target, and the official "
                "About page only. No guessed ATS slug, LinkedIn board, or parent-company board was queried."
            ),
        })

        # 1) The exact reviewed redirect journey.
        try:
            self.page_guard()
            initial_response = await self.http.get(OFFICIAL_ENTRY, headers=PUBLIC_HEADERS)
            initial_history = validate_entry_journey(initial_response)
            initial_parsed = parse_homepage(initial_response.text, str(initial_response.url))
        except Exception as exc:
            self.result.errors.append(f"oyo_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["oyo_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_entry"] = _entry_evidence(initial_response)
        self.result.metadata["redirect_chain"] = initial_history
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "h1": initial_parsed["h1"],
            "headings": initial_parsed["headings"],
            "anchor_count": initial_parsed["anchor_count"],
            "career_links": initial_parsed["career_links"],
            "aggregator_links": initial_parsed["aggregator_links"],
            "external_ats_links": initial_parsed["external_ats_links"],
            "job_listing_candidates": initial_parsed["job_listing_candidates"],
            "internal_career_nav": initial_parsed["internal_career_nav"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "visible_text_chars": initial_parsed["visible_text_chars"],
            "normalized_text_chars": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256": initial_parsed["normalized_text_sha256"],
        }

        if not initial_parsed.get("h1_matches_reviewed"):
            self.result.warnings.append("oyo_homepage_h1_variant")
        if has_listing_signal(initial_parsed):
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_listing"] = {
                "job_listing_candidates": initial_parsed["job_listing_candidates"],
                "external_ats_links": initial_parsed["external_ats_links"],
                "ats_embeds": initial_parsed["ats_embeds"],
                "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            }
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["oyo_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        linkedin = [item for item in initial_parsed["aggregator_links"]
                    if _same_url(item["url"], LINKEDIN_CAREERS)]
        if not linkedin:
            self.result.warnings.append("career_destination_is_not_the_reviewed_linkedin_link")
            if not initial_parsed["career_links"]:
                self.result.warnings.append("no_career_link_in_homepage_variant")
        else:
            self.result.warnings.append("career_destination_is_linkedin_aggregator")
        if initial_parsed["internal_career_nav"]:
            self.result.warnings.append("internal_careers_nav_still_present_but_dead")

        # 2) Final structural recheck of the same journey.
        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response = await self.http.get(OFFICIAL_ENTRY, headers=PUBLIC_HEADERS)
            validate_entry_journey(final_response)
            final_parsed = parse_homepage(final_response.text, str(final_response.url))
            self.result.metadata["final_recheck"] = _entry_evidence(final_response)
        except Exception as exc:
            self.result.errors.append(f"oyo_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["oyo_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        if final_parsed is not None:
            if has_listing_signal(final_parsed):
                self.result.errors.append("unreviewed_careers_listing_detected")
            if _evidence(initial_parsed) != _evidence(final_parsed):
                self.result.errors.append("oyo_homepage_structure_changed_between_requests")
            elif initial_parsed["normalized_text_sha256"] != final_parsed["normalized_text_sha256"]:
                self.result.warnings.append("oyo_homepage_visible_text_changed_between_requests")

        # 3) The official About page independently confirms the same single CTA.
        about_started = time.perf_counter()
        about_parsed = None
        try:
            self.page_guard()
            about_response = await self.http.get(OFFICIAL_ABOUT, headers=PUBLIC_HEADERS)
            about_parsed = parse_about(about_response.text, str(about_response.url))
            self.result.metadata["about_page"] = {
                "requested_url": OFFICIAL_ABOUT,
                "final_url": str(about_response.url),
                "status_code": about_response.status_code,
                "html_bytes": len(about_response.content),
                "html_sha256": sha256(about_response.content).hexdigest(),
                "redirects": redirect_chain(about_response),
                "fetched_at": now(),
                "title": about_parsed["title"],
                "career_links": about_parsed["career_links"],
                "external_ats_links": about_parsed["external_ats_links"],
                "job_listing_candidates": about_parsed["job_listing_candidates"],
                "job_posting_schema_count": about_parsed.get("job_posting_schema_count", 0),
                "visible_text_chars": about_parsed["visible_text_chars"],
                "normalized_text_sha256": about_parsed["normalized_text_sha256"],
            }
            if has_listing_signal(about_parsed):
                self.result.errors.append("unreviewed_careers_listing_detected_on_about_page")
            about_linkedin = [item for item in about_parsed["aggregator_links"]
                              if _same_url(item["url"], LINKEDIN_CAREERS)]
            if about_linkedin:
                self.result.warnings.append("about_page_confirms_linkedin_aggregator")
        except Exception as exc:
            self.result.warnings.append(f"oyo_about_page_unavailable: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["oyo_about_check_seconds"] = max(0.0, time.perf_counter() - about_started)

        # The empty result is an explicit removed-route/no-listing outcome.
        if "unreviewed_careers_listing_detected" not in self.result.errors:
            self.result.errors.extend([
                "official_careers_route_removed",
                "no_public_job_listing",
            ])
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The ledger /careers/ URL is a 301 to the OYO hotel homepage. The homepage and the "
            "official About page publish only a LinkedIn jobs link (aggregator, excluded); there are "
            "no job cards, job paths, listing requests, filters, pagination, ATS embeds or JobPosting "
            "JSON-LD on the official pages. The empty result is an explicit no-listing outcome, not a "
            "successful zero-job listing."
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["oyo_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
