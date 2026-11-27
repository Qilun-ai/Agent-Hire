"""Krutrim's inspected official careers journey, currently without a listing.

The ledger entry ``https://www.olakrutrim.com/careers`` is HTTP 404 on the
current Krutrim Cloud site; the rendered page lands on the cloud sign-in screen.
The native homepage and published sitemap expose no careers or jobs route. The
separate official Krutrim AI Labs page publishes one *Open Positions* action
that opens ``https://www.linkedin.com/company/krutrim/jobs/``. LinkedIn is an
aggregator (and returned a Cloudflare 403 in the independent browser visit), so
it cannot define Krutrim company membership here.

This adapter rechecks that exact journey, records failures precisely, and never
substitutes a zero-job result or a parent/aggregator board for an inaccessible
company listing. A changed successful listing requires a fresh individual
review; it is never collected by this unavailable-journey adapter.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter


ENTRY = "https://www.olakrutrim.com/careers"
HOME = "https://www.olakrutrim.com/"
SITEMAP = "https://www.olakrutrim.com/sitemap.xml"
AI_LABS = "https://ai-labs.olakrutrim.com/"
LINKEDIN_JOBS = "https://www.linkedin.com/company/krutrim/jobs/"
KRUTRIM_AI_CAREERS = "https://krutrim.ai/careers"

CAREERS_PATH = re.compile(r"(?:^|/)(?:careers?|jobs?|hiring|join)(?:/|$)", re.I)
JOB_DETAIL_PATH = re.compile(r"(?:/jobs?/view/|/jobs?/|/careers?/|jobId=|gh_jid=|/job/)", re.I)
NEXT_PAGE_CHUNK = re.compile(r"/_next/static/chunks/.*?/?page-[^/]+\.js$", re.I)


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _official_url(url: str, host: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() == host.lower()


def _page_links(html: str, response_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        rows.append({"text": normalized(anchor.get_text(" ", strip=True)), "url": target})
    return rows


def parse_official_entry(html: str, *, status: int, response_url: str = ENTRY) -> dict:
    """Validate the known missing Krutrim careers route."""
    if not _official_url(response_url, "www.olakrutrim.com") or urlsplit(response_url).path.rstrip("/") != "/careers":
        raise ValueError("Krutrim official entry redirected or resolved to an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    next_error = soup.find("html", id="__next_error__") is not None
    links = _page_links(html, response_url)
    job_links = [row for row in links if JOB_DETAIL_PATH.search(row["url"])]
    return {
        "request_url": ENTRY,
        "response_url": response_url,
        "status": status,
        "title": title,
        "nextjs_error_page": next_error,
        "job_links": job_links,
        "listing_rows": 0,
        "observed_missing_route": status == 404 and next_error,
    }


def parse_official_home(html: str, response_url: str = HOME) -> dict:
    if not _official_url(response_url, "www.olakrutrim.com") or urlsplit(response_url).path not in ("", "/"):
        raise ValueError("Krutrim official home resolved to an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    links = _page_links(html, response_url)
    careers_links = [row for row in links
                     if re.search(r"careers?|jobs?|hiring|join us", row["text"], re.I)
                     or CAREERS_PATH.search(urlsplit(row["url"]).path)]
    return {
        "request_url": HOME,
        "response_url": response_url,
        "title": title,
        "careers_links": careers_links,
        "careers_navigation_exposed": bool(careers_links),
    }


def parse_sitemap(xml: str) -> dict:
    soup = BeautifulSoup(xml, "xml")
    locations = [normalized(node.get_text()) for node in soup.find_all("loc")]
    locations = [value for value in locations if value]
    careers_urls = [url for url in locations if CAREERS_PATH.search(urlsplit(url).path)]
    return {
        "locations": locations,
        "careers_urls": careers_urls,
        "careers_route_published": bool(careers_urls),
    }


def parse_ai_labs_page(html: str, response_url: str = AI_LABS) -> dict:
    if not _official_url(response_url, "ai-labs.olakrutrim.com"):
        raise ValueError("Krutrim AI Labs resolved to an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    text = normalized(soup.get_text(" ", strip=True))
    links = _page_links(html, response_url)
    job_links = [row for row in links if JOB_DETAIL_PATH.search(row["url"])]
    controls = [normalized(node.get_text(" ", strip=True)) for node in soup.select("button")]
    open_positions = [label for label in controls if label.lower() == "open positions"]
    chunks = [urljoin(response_url, node.get("src", "")) for node in soup.select("script[src]")
              if "/_next/static/" in (node.get("src") or "")]
    page_chunks = [url for url in chunks if NEXT_PAGE_CHUNK.search(urlsplit(url).path)]
    return {
        "request_url": AI_LABS,
        "response_url": response_url,
        "title": title,
        "career_heading_present": "Career Opportunities" in text,
        "open_positions_controls": open_positions,
        "job_links": job_links,
        "listing_rows": 0,
        "page_chunks": page_chunks,
    }


def parse_ai_labs_careers_action(js: str, response_url: str) -> dict:
    if not _official_url(response_url, "ai-labs.olakrutrim.com"):
        raise ValueError("Krutrim AI Labs page chunk came from an unexpected host")
    target = re.search(r"https://www\.linkedin\.com/company/krutrim/jobs/?", js)
    return {
        "chunk_url": response_url,
        "linkedin_jobs_target": target.group(0) if target else None,
        "has_open_positions_handler": "open_positions_clicked" in js,
        "aggregator_job_detail_links": sorted(set(re.findall(r"/jobs/view/\d+", js))),
    }


def parse_linkedin_page(html: str, *, status: int, final_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    text = normalized(soup.get_text(" ", strip=True)).lower()
    links = _page_links(html, final_url)
    job_links = [row for row in links if "/jobs/view/" in row["url"]]
    blocked = (status in (401, 403, 429)
               or "you have been blocked" in text
               or "attention required" in title.lower())
    return {
        "status": status,
        "final_url": final_url,
        "title": title,
        "blocked": blocked,
        "job_links": job_links,
        "exposed_job_cards": len(job_links),
        "profile_only": "linkedin.com/company/krutrim" in final_url and not job_links,
    }


def _http_status(error: str) -> int | None:
    match = re.match(r"HTTP (\d{3}):", error or "")
    return int(match.group(1)) if match else None


class Krutrim(Adapter):
    VERIFICATION_REVISION = "krutrim.official-404-linkedin-blocked.1"

    def _failed_source(self, url: str, reason: str):
        self.result.metadata.setdefault("failed_sources", []).append({"url": url, "reason": reason})

    async def _fetch(self, url: str):
        if "page_budget_exhausted" in self.result.errors:
            return None
        if not self.page_guard():
            return None
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            self.result.metadata["journey"].append({
                "url": url, "status": response.status_code, "final_url": str(response.url),
                "bytes": len(response.content), "seconds": seconds, "error": None,
            })
            return response
        except FetchError as exc:
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            status = _http_status(str(exc))
            self.result.metadata["journey"].append({
                "url": url, "status": status, "final_url": None,
                "bytes": None, "seconds": seconds, "error": str(exc),
            })
            return None

    def _last(self) -> dict:
        journey = self.result.metadata.get("journey") or []
        return journey[-1] if journey else {}

    def _changed(self):
        self.result.metadata["journey_outcome"] = "changed_requires_individual_membership_review"
        self.result.errors.append("krutrim_official_journey_changed_requires_individual_membership_review")
        return self.result

    def _parse(self, parser, *args, **kwargs):
        try:
            return parser(*args, **kwargs)
        except ValueError as exc:
            self.result.metadata["parser_error"] = f"{parser.__name__}: {exc}"
            return None

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            official_home=HOME,
            official_sitemap=SITEMAP,
            ai_labs_entry=AI_LABS,
            alternate_branded_domain=KRUTRIM_AI_CAREERS,
            published_open_positions_action=None,
            published_careers_destination=None,
            published_careers_destination_url=LINKEDIN_JOBS,
            company_membership_verified=False,
            ats_board_requests=0,
            exact_job_ids_exposed=[],
            listed_job_rows=0,
            journey=[],
            failed_sources=[],
            review_evidence="companies/krutrim/journey-research.md",
            rendered_reference="companies/krutrim/browser-reference-worker.json",
            scope_note=("A LinkedIn company page, the Ola/ANI TurboHire board, or a parent-group "
                        "vacancy set does not establish Krutrim company membership."),
        )
        self.result.expected_total = None
        self.result.listing_complete = False

        # 1. The ledger's official route must be the known missing Next.js route.
        await self._fetch(ENTRY)
        entry = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if entry.get("status") is None:
            self._failed_source(ENTRY, entry.get("error", "request failed"))
            self.result.errors.append("krutrim_official_entry_unavailable_before_membership_review")
            return self.result
        self.result.metadata["official_entry_status"] = entry["status"]
        if entry["status"] != 404:
            self._failed_source(ENTRY, f"expected HTTP 404; observed HTTP {entry['status']}")
            self.result.errors.append("krutrim_official_journey_changed_requires_individual_membership_review")
            return self.result
        self._failed_source(ENTRY, "HTTP 404; rendered route lands on the cloud sign-in screen, not a public listing")

        # 2. The current official homepage and published sitemap expose no careers route.
        home_response = await self._fetch(HOME)
        home = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if home_response is None or home.get("status") != 200:
            self._failed_source(HOME, home.get("error") or f"HTTP {home.get('status')}")
            return self._changed()
        parsed_home = self._parse(parse_official_home, home_response.text, str(home_response.url))
        if parsed_home is None or parsed_home["title"] != "Krutrim Cloud":
            self._failed_source(HOME, "official home title or final URL changed")
            return self._changed()
        self.result.metadata["official_home_has_careers_navigation"] = parsed_home["careers_navigation_exposed"]
        if parsed_home["careers_navigation_exposed"]:
            self._failed_source(HOME, "careers/jobs navigation appeared")
            return self._changed()

        sitemap_response = await self._fetch(SITEMAP)
        sitemap = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if sitemap_response is None or sitemap.get("status") != 200:
            self._failed_source(SITEMAP, sitemap.get("error") or f"HTTP {sitemap.get('status')}")
            return self._changed()
        parsed_sitemap = self._parse(parse_sitemap, sitemap_response.text)
        if (parsed_sitemap is None or not parsed_sitemap["locations"]
                or "xml" not in (sitemap_response.headers.get("content-type") or "").lower()
                or str(sitemap_response.url) != SITEMAP):
            self._failed_source(SITEMAP, "published sitemap content or final URL changed")
            return self._changed()
        self.result.metadata["official_sitemap_careers_routes"] = parsed_sitemap["careers_urls"]
        self.result.metadata["official_sitemap_location_count"] = len(parsed_sitemap["locations"])
        if parsed_sitemap["careers_route_published"]:
            self._failed_source(SITEMAP, "careers route appeared in sitemap")
            return self._changed()

        # 3. The official AI Labs page must publish exactly the observed LinkedIn action.
        ai_response = await self._fetch(AI_LABS)
        ai_labs = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if ai_response is None or ai_labs.get("status") != 200:
            self._failed_source(AI_LABS, ai_labs.get("error") or f"HTTP {ai_labs.get('status')}")
            return self._changed()
        parsed_ai = self._parse(parse_ai_labs_page, ai_response.text, str(ai_response.url))
        if parsed_ai is None or "Krutrim AI Labs" not in parsed_ai["title"]:
            self._failed_source(AI_LABS, "AI Labs title or final URL changed")
            return self._changed()
        self.result.metadata["ai_labs"] = {
            "title": parsed_ai["title"],
            "career_heading_present": parsed_ai["career_heading_present"],
            "open_positions_controls": parsed_ai["open_positions_controls"],
            "job_links": parsed_ai["job_links"],
            "page_chunks": parsed_ai["page_chunks"],
        }
        if parsed_ai["job_links"] or not parsed_ai["career_heading_present"] or not parsed_ai["open_positions_controls"]:
            return self._changed()
        if not parsed_ai["page_chunks"]:
            self._failed_source(AI_LABS, "shipped careers page chunk not found")
            return self._changed()

        chunk_url = parsed_ai["page_chunks"][0]
        chunk_response = await self._fetch(chunk_url)
        chunk = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if chunk_response is None or chunk.get("status") != 200:
            self._failed_source(chunk_url, chunk.get("error") or f"HTTP {chunk.get('status')}")
            return self._changed()
        action = self._parse(parse_ai_labs_careers_action, chunk_response.text, str(chunk_response.url))
        if action is None:
            self._failed_source(chunk_url, "shipped page chunk final URL changed")
            return self._changed()
        self.result.metadata["ai_labs_careers_action"] = action
        if not action["linkedin_jobs_target"] or not action["has_open_positions_handler"]:
            self._failed_source(chunk_url, "Open Positions handler or LinkedIn target changed")
            return self._changed()
        self.result.metadata["published_open_positions_action"] = action["linkedin_jobs_target"]

        # 4. The exact published destination is an aggregator; verify accessibility only.
        linkedin_response = await self._fetch(action["linkedin_jobs_target"])
        linkedin = self._last()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if linkedin.get("status") is None:
            destination = {"status": None, "blocked": True, "profile_only": False,
                           "exposed_job_cards": 0, "final_url": None,
                           "reason": linkedin.get("error", "request failed")}
            self._failed_source(action["linkedin_jobs_target"], destination["reason"])
        elif linkedin["status"] != 200:
            destination = {"status": linkedin["status"], "blocked": linkedin["status"] in (401, 403, 429),
                           "profile_only": False, "exposed_job_cards": 0,
                           "final_url": linkedin.get("final_url"),
                           "reason": linkedin.get("error", f"HTTP {linkedin['status']}")}
            self._failed_source(action["linkedin_jobs_target"], destination["reason"])
        else:
            parsed_li = parse_linkedin_page(
                linkedin_response.text, status=linkedin["status"],
                final_url=linkedin.get("final_url") or action["linkedin_jobs_target"],
            )
            destination = {
                "status": parsed_li["status"],
                "blocked": parsed_li["blocked"],
                "profile_only": parsed_li["profile_only"],
                "exposed_job_cards": parsed_li["exposed_job_cards"],
                "final_url": parsed_li["final_url"],
                "title": parsed_li["title"],
                "reason": ("aggregator page exposed job cards but is not company-scoped membership"
                          if parsed_li["exposed_job_cards"] else
                          "aggregator company profile page exposed no Krutrim job cards"),
            }
        self._failed_source(action["linkedin_jobs_target"], destination["reason"])
        self.result.metadata["published_careers_destination"] = destination
        self.result.metadata["listed_job_rows"] = 0
        self.result.metadata["company_membership_verified"] = False

        self.result.errors.append(
            "krutrim_official_careers_404_and_only_published_action_is_linkedin_aggregator"
        )
        return self.result
