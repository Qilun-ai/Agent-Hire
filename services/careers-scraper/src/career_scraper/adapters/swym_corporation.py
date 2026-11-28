"""SWYM Corporation (Swym) — official careers journey, reviewed 2026-11-08.

The ledger entry ``https://swym.it/careers/`` is a removed route on the legacy
``swym.it`` host: HTTP 404, empty body, ``Server: awselb/2.0``,
``Wafrule: 5``, no redirect.  The same host's root 301s to ``getswym.com`` which
301s to ``https://www.getswym.com/`` — the live Swym site — and that site's own
navigation publishes ``/careers``.

``https://www.getswym.com/careers`` is a Webflow marketing page: no job cards,
no listing request, no filters, no pagination, no ``JobPosting`` JSON-LD and no
ATS embed.  Its only openings journey is two "View Current Openings" buttons to
``https://wellfound.com/company/swym/jobs``.

That destination currently answers with a Cloudflare interactive challenge
(HTTP 403, ``cf-mitigated: challenge``, "Security Check | Wellfound") to plain
HTTP *and* to headless Chromium, and its legacy ``angel.co`` alias 301s into the
same challenge.  Membership therefore cannot be observed, and is never guessed:
this adapter records the exact block, collects zero fabricated records and
leaves ``expected_total`` null.  It also collects native postings if Swym ever
publishes them on its own careers page (JSON-LD ``JobPosting`` or company-hosted
detail links), so the flow recovers without a rewrite.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import access
from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://swym.it/careers/"          # ledger entry: removed route, 404
OFFICIAL_ORIGIN = "https://swym.it/"                 # 301 -> getswym.com -> www.getswym.com
CAREERS_URL = "https://www.getswym.com/careers"      # live official careers page
SITEMAP_URL = "https://www.getswym.com/sitemap.xml"
OFFICIAL_HOSTS = {"swym.it", "getswym.com", "www.getswym.com"}
CAREER_PATH = re.compile(r"(?i)^/(?:careers?|jobs?|join(?:-us)?|open-positions|positions|opportunities)(?:/|$)")

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
    r"phenompeople\.com|eightfold\.ai|comeet\.co|teamtailor\.com|breezy\.hr|kula\.ai"
    r")$"
)
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"linkedin\.com|indeed\.com|naukri\.com|glassdoor\.[a-z.]+|"
    r"wellfound\.com|angel\.co|instahyre\.com|cutshort\.io|foundit\.in|"
    r"monster\.com|dice\.com|ziprecruiter\.com|simplyhired\.com|bayt\.com|"
    r"seek\.com|jobsdb\.com|jobstreet\.com"
    r")$"
)
SOCIAL_HOST = re.compile(
    r"(?i)(?:^|\.)(?:facebook\.com|youtube\.com|youtu\.be|x\.com|twitter\.com|"
    r"instagram\.com|tiktok\.com|threads\.net|pinterest\.com|medium\.com|substack\.com)$"
)
OPENINGS_TEXT = re.compile(
    r"(?i)(?:view|see|explore|browse|check)?\s*(?:our\s+)?(?:current\s+|open\s+)?"
    r"(?:openings?|roles?|positions?|jobs?|vacancies|opportunities)|apply\s+now|join\s+(?:us|our\s+team)"
)
PAGINATION_TEXT = re.compile(
    r"(?i)^(?:next(?: page)?|previous(?: page)?|prev(?: page)?|load more|show more|page \d+)\s*[»›→]?$"
)
CHALLENGE_MARKERS = (
    "cf_chl", "challenge-platform", "just a moment", "security check",
    "verify your request", "checking your browser",
)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def is_official_host(url: str) -> bool:
    return _host(url) in OFFICIAL_HOSTS


def link_kind(url: str) -> str:
    host = _host(url)
    if ATS_HOST.search(host):
        return "ats"
    if AGGREGATOR_HOST.search(host):
        return "aggregator"
    if host in OFFICIAL_HOSTS:
        return "official"
    return "external"


def anchors(soup: BeautifulSoup, response_url: str) -> list[dict]:
    result = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        absolute = urljoin(response_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        text = normalize_text(anchor.get_text(" ", strip=True))
        result.append({
            "text": text,
            "href": href,
            "url": absolute,
            "host": _host(absolute),
            "path": _path(absolute),
            "kind": link_kind(absolute),
            "opens_new_tab": (anchor.get("target") or "").lower() == "_blank",
        })
    return result


def _walk_jsonld(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_jsonld(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_jsonld(item)


def jsonld_jobpostings(soup: BeautifulSoup) -> list[dict]:
    jobs = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in _walk_jsonld(data):
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types:
                jobs.append(item)
    return jobs


def _date(value) -> str | None:
    """Keep a source date only when the source actually published one."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def _location_names(item: dict) -> list[str]:
    raw = item.get("jobLocation")
    raw = raw if isinstance(raw, list) else [raw]
    names = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            parts = [address.get(key) for key in (
                "addressLocality", "addressRegion", "addressCountry")]
            name = ", ".join(str(part) for part in parts if part)
        else:
            name = normalize_text(entry.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _identifier(item: dict) -> str | None:
    value = item.get("identifier")
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def job_from_jsonld(item: dict, page_url: str) -> dict:
    """Map one published JobPosting object without inventing unknown fields."""
    employer = item.get("hiringOrganization")
    if isinstance(employer, dict):
        employer = employer.get("name") or employer
    description = item.get("description") or ""
    return {
        "title": normalize_text(item.get("title")),
        "job_id": _identifier(item),
        "url": urljoin(page_url, item.get("url") or "") if item.get("url") else page_url,
        "description_html": description if isinstance(description, str) else "",
        "date_posted": _date(item.get("datePosted")),
        "date_posted_raw": item.get("datePosted"),
        "date_updated": _date(item.get("dateModified")),
        "valid_through": _date(item.get("validThrough")),
        "employment_type": item.get("employmentType"),
        "locations": _location_names(item),
        "salary": item.get("baseSalary"),
        "employer": employer,
        "qualifications": item.get("qualifications"),
        "responsibilities": item.get("responsibilities"),
        "experience": item.get("experienceRequirements"),
        "education": item.get("educationRequirements"),
        "raw": item,
    }


def native_job_links(links: list[dict]) -> list[dict]:
    """Company-hosted detail links only (never the /careers root itself)."""
    listing_tails = {
        "jobs", "job", "careers", "career", "roles", "positions", "position",
        "openings", "opening", "open-roles", "open-positions", "vacancies",
        "join-us", "join", "all", "index",
    }
    result = []
    seen = set()
    for link in links:
        if link["kind"] != "official":
            continue
        path = link["path"]
        if path == "/careers" or not CAREER_PATH.match(path):
            continue
        # /careers/jobs, /careers/openings etc. are listing routes, not a posting.
        tail = path.strip("/").split("/")
        if len(tail) < 2 or tail[-1].lower() in listing_tails:
            continue
        if not link["text"]:
            continue
        if path in seen:
            continue
        seen.add(path)
        result.append(link)
    return result


def openings_links(links: list[dict]) -> list[dict]:
    """External listing destinations explicitly published by the careers page."""
    result = []
    seen = set()
    for link in links:
        if link["kind"] in ("ats", "aggregator"):
            plausible = bool(
                CAREER_PATH.match(link["path"])
                or "/jobs" in link["path"]
                or (link["path"] in ("", "/") and OPENINGS_TEXT.search(link["text"]))
            )
            signal = plausible
        elif link["kind"] == "external" and not SOCIAL_HOST.search(link["host"]):
            plausible = bool(OPENINGS_TEXT.search(link["text"]))
            signal = plausible
        else:
            continue
        if not (plausible and signal):
            continue
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        result.append(link)
    return result


def pagination_controls(soup: BeautifulSoup) -> list[str]:
    result = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and len(text) <= 40 and PAGINATION_TEXT.match(text):
            result.append(text)
    return result


def parse_homepage(html: str, response_url: str) -> dict:
    if not is_official_host(response_url):
        raise ValueError(f"Swym homepage redirected to an unexpected host: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    links = anchors(soup, response_url)
    careers = [link for link in links
               if link["kind"] == "official" and CAREER_PATH.match(link["path"])]
    return {
        "response_url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "careers_links": careers,
        "careers_url": careers[0]["url"] if careers else None,
        "anchor_count": len(links),
    }


def parse_careers_page(html: str, response_url: str = CAREERS_URL) -> dict:
    if not is_official_host(response_url):
        raise ValueError(f"Swym careers page came from an unexpected host: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    links = anchors(soup, response_url)
    postings = jsonld_jobpostings(soup)
    native = native_job_links(links)
    openings = openings_links(links)
    page_text = visible_text(html)
    body = normalize_text(page_text)
    return {
        "response_url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h1": [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")],
        "anchor_count": len(links),
        "native_job_links": native,
        "openings_links": openings,
        "job_posting_count": len(postings),
        "job_postings": postings,
        "pagination_controls": pagination_controls(soup),
        "has_native_listing": bool(native or postings or pagination_controls(soup)),
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body),
        "normalized_text_sha256": sha256(body.encode()).hexdigest(),
        "evidence_links": {
            "official": [link for link in links if link["kind"] == "official"][:40],
            "ats": [link for link in links if link["kind"] == "ats"],
            "aggregator": [link for link in links if link["kind"] == "aggregator"],
        },
    }


def challenge_markers(status_code: int, headers: dict, body: str) -> dict:
    lowered = (body or "")[:200_000].lower()
    found = [marker for marker in CHALLENGE_MARKERS if marker in lowered]
    mitigated = (headers.get("cf-mitigated") or "").lower()
    server = (headers.get("server") or "").lower()
    is_challenge = bool(found) or "challenge" in mitigated
    return {
        "is_challenge": is_challenge,
        "body_markers": found,
        "cf_mitigated": headers.get("cf-mitigated"),
        "server": headers.get("server") or None,
        "cloudflare": "cloudflare" in server,
        "challenge_title": _title(body or ""),
        "status_code": status_code,
    }


def _title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return normalize_text(unescape(match.group(1))) if match else ""


def classify_destination(status_code: int, headers: dict, body: str) -> dict:
    marks = challenge_markers(status_code, headers, body)
    if marks["is_challenge"]:
        state = "cloudflare_challenge"
    elif status_code == 429:
        state = "rate_limited"
    elif status_code == 403:
        state = "forbidden"
    elif status_code >= 500:
        state = "server_error"
    elif status_code == 200:
        state = "ok"
    else:
        state = "http_error"
    return {"state": state, **marks}


def parse_detail_page(html: str, response_url: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    postings = jsonld_jobpostings(soup)
    if postings:
        return {"state": "jobposting_jsonld",
                "job": job_from_jsonld(postings[0], response_url),
                "jsonld_count": len(postings)}
    main = soup.select_one("main") or soup.body
    page_text = visible_text(str(main)) if main else ""
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    return {
        "state": "no_jobposting_jsonld",
        "title": title,
        "description_text": page_text,
        "description_chars": len(normalize_text(page_text)),
    }


def response_summary(response) -> dict:
    return {
        "requested_url": str(response.request.url),
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "bytes": len(response.content),
        "sha256": sha256(response.content).hexdigest(),
        "redirects": [
            {"status": item.status_code, "url": str(item.url),
             "location": item.headers.get("location")}
            for item in response.history
        ],
        "headers": {
            key: response.headers.get(key) for key in (
                "server", "cf-mitigated", "cf-ray", "cf-cache-status",
                "content-type", "wafrule", "location")
            if response.headers.get(key) is not None
        },
        "fetched_at": now(),
    }


async def fetch_capture(http, url: str):
    """Keep the raw response even for 4xx/5xx: the status is the evidence."""
    request = http.client.build_request("GET", url)
    response = await http.client.send(request, follow_redirects=True)
    http._save(response, "GET", None)
    return response


class Swym(Adapter):
    """Read Swym's official careers journey and record blocked membership."""

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "official_origin_url": OFFICIAL_ORIGIN,
            "careers_url": CAREERS_URL,
            "sitemap_url": SITEMAP_URL,
            "listing_authority": (
                "www.getswym.com/careers (the live official site; the ledger host swym.it "
                "301s to getswym.com and its /careers/ path is a removed 404 route)"
            ),
            "detail_method": (
                "company-hosted JobPosting JSON-LD / careers detail links when published; "
                "the published Wellfound openings destination is not scraped because it is "
                "an aggregator host and currently Cloudflare-challenged"
            ),
            "request_scope": (
                "official swym.it -> www.getswym.com journey and the one openings URL the "
                "careers page publishes; no guessed ATS slug or aggregator search"
            ),
        })

        entry_started = time.perf_counter()
        try:
            self.page_guard()
            entry_response = await fetch_capture(self.http, OFFICIAL_ENTRY)
            entry = response_summary(entry_response)
        except Exception as exc:
            self.result.errors.append(f"swym_ledger_entry_failed: {type(exc).__name__}: {exc}")
            self._finish_unavailable(source_started)
            return self.result
        finally:
            self.result.timings["swym_entry_check_seconds"] = max(0.0, time.perf_counter() - entry_started)
        self.result.metadata["ledger_entry"] = entry

        homepage_started = time.perf_counter()
        try:
            self.page_guard()
            homepage_response = await self.http.get(OFFICIAL_ORIGIN)
            homepage = parse_homepage(homepage_response.text, str(homepage_response.url))
            self.result.metadata["homepage"] = {
                **response_summary(homepage_response),
                "title": homepage["title"],
                "careers_link": homepage["careers_url"],
                "anchor_count": homepage["anchor_count"],
            }
            careers_url = homepage["careers_url"] or CAREERS_URL
        except Exception as exc:
            self.result.errors.append(f"swym_homepage_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["homepage"] = {"error": f"{type(exc).__name__}: {exc}"}
            careers_url = CAREERS_URL
        finally:
            self.result.timings["swym_homepage_check_seconds"] = max(0.0, time.perf_counter() - homepage_started)

        careers_started = time.perf_counter()
        try:
            self.page_guard()
            careers_response = await self.http.get(careers_url)
        except Exception as exc:
            self.result.errors.append(f"swym_careers_page_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["careers_page"] = {"error": f"{type(exc).__name__}: {exc}"}
            self._finish_unavailable(source_started)
            self.result.timings["swym_careers_page_seconds"] = max(0.0, time.perf_counter() - careers_started)
            return self.result
        finally:
            self.result.timings["swym_careers_page_seconds"] = max(0.0, time.perf_counter() - careers_started)
        self.result.metadata["careers_page_response"] = response_summary(careers_response)
        try:
            careers = parse_careers_page(careers_response.text, str(careers_response.url))
        except Exception as exc:
            self.result.errors.append(f"swym_careers_parse_failed: {type(exc).__name__}: {exc}")
            self._finish_unavailable(source_started)
            return self.result
        self.result.metadata["careers_page"] = {
            "response_url": careers["response_url"],
            "title": careers["title"],
            "h1": careers["h1"],
            "native_job_links": careers["native_job_links"],
            "openings_links": careers["openings_links"],
            "job_posting_count": careers["job_posting_count"],
            "pagination_controls": careers["pagination_controls"],
            "visible_text_chars": careers["visible_text_chars"],
            "normalized_text_chars": careers["normalized_text_chars"],
            "normalized_text_sha256": careers["normalized_text_sha256"],
            "has_native_listing": careers["has_native_listing"],
        }

        # 1) Company-hosted postings, when the company publishes them.
        if careers["job_posting_count"]:
            for item in careers["job_postings"]:
                self._add_jsonld_job(item, careers["response_url"])
        elif careers["native_job_links"]:
            await self._collect_native_links(careers["native_job_links"])

        # 2) The external openings destination(s): inspect; a challenge is escalated.
        probes = await self._probe_openings(careers["openings_links"])
        self.result.metadata["openings_probe"] = probes

        challenge = [probe for probe in probes if probe["state"] == "cloudflare_challenge"]
        unreviewed = [probe for probe in probes if probe["state"] == "ok"]
        if careers["has_native_listing"]:
            self.result.metadata["native_listing"] = {
                "jobs": len(self.result.jobs),
                "pagination_controls": careers["pagination_controls"],
                "expected_total": len(self.result.jobs) if not careers["pagination_controls"] else None,
            }
            self.result.expected_total = (
                len(self.result.jobs) if not careers["pagination_controls"] else None)
            self.result.listing_complete = bool(
                self.result.jobs and not careers["pagination_controls"]
                and not any(error.endswith("_failed") for error in self.result.errors))
            if not self.result.listing_complete:
                self.result.errors.append("native_listing_membership_not_closed")
            if challenge:
                self.result.errors.append("published_openings_destination_cloudflare_challenge")
        else:
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.errors.append("official_careers_page_has_no_native_postings")
            if challenge:
                self.result.errors.append("published_openings_destination_cloudflare_challenge")
                self.result.errors.append("company_listing_not_reachable")
            elif unreviewed:
                self.result.errors.append("unreviewed_openings_destination_listing")
            else:
                self.result.errors.append("no_public_job_listing")

        self.result.metadata["membership_conclusion"] = (
            "Swym's live careers page publishes no company-hosted postings. Its only "
            "openings journey is https://wellfound.com/company/swym/jobs, which returns "
            "a Cloudflare interactive challenge to plain HTTP and headless Chromium, so "
            "membership is unobserved (expected_total null); zero collected is not zero "
            "vacancies."
            if not careers["has_native_listing"] else
            "Swym's live careers page publishes company-hosted postings; see native_listing."
        )
        self._finish_unavailable(source_started)
        return self.result

    def _add_jsonld_job(self, item: dict, page_url: str):
        fields = job_from_jsonld(item, page_url)
        if not fields["title"]:
            self.result.errors.append("jobposting_without_title")
            return
        job = self.job(
            fields["raw"], title=fields["title"], url=fields["url"],
            job_id=fields["job_id"], description=fields["description_html"],
            method="company_jobposting_jsonld", description_url=page_url,
            locations=fields["locations"], employment_type=fields["employment_type"],
            date_posted=fields["date_posted"], date_posted_raw=fields["date_posted_raw"],
            date_updated=fields["date_updated"], valid_through=fields["valid_through"],
            salary=fields["salary"], employer=fields["employer"],
            qualifications=fields["qualifications"],
            responsibilities=fields["responsibilities"],
            experience=fields["experience"], education=fields["education"],
            apply_url=None,
        )
        self.add(job)

    async def _collect_native_links(self, links: list[dict]):
        for index, link in enumerate(links):
            try:
                self.page_guard()
                response = await self.http.get(link["url"])
            except Exception as exc:
                self.result.errors.append(
                    f"native_detail_failed:{link['url']}: {type(exc).__name__}: {exc}")
                continue
            parsed = parse_detail_page(response.text, str(response.url))
            if parsed["state"] == "jobposting_jsonld":
                fields = parsed["job"]
                job = self.job(
                    fields["raw"], title=fields["title"] or link["text"],
                    url=fields["url"] or link["url"], job_id=fields["job_id"],
                    description=fields["description_html"],
                    method="company_jobposting_jsonld",
                    description_url=str(response.url),
                    locations=fields["locations"],
                    employment_type=fields["employment_type"],
                    date_posted=fields["date_posted"],
                    date_posted_raw=fields["date_posted_raw"],
                    date_updated=fields["date_updated"],
                    valid_through=fields["valid_through"], salary=fields["salary"],
                    employer=fields["employer"],
                    qualifications=fields["qualifications"],
                    responsibilities=fields["responsibilities"],
                    experience=fields["experience"], education=fields["education"],
                )
            else:
                text = parsed.get("description_text") or ""
                if len(text) < 100:
                    self.result.errors.append(
                        f"native_detail_without_description:{link['url']}")
                    continue
                job = self.job(
                    {"detail_url": link["url"]}, title=link["text"] or parsed["title"],
                    url=link["url"], description=text,
                    method="company_page_main_text",
                    description_url=str(response.url),
                )
                job.warnings.append("unverified_main_text")
            self.add(job)

    async def _probe_openings(self, links: list[dict]) -> list[dict]:
        probes = []
        for link in links:
            started = time.perf_counter()
            try:
                self.page_guard()
                response = await fetch_capture(self.http, link["url"])
                classification = classify_destination(
                    response.status_code, dict(response.headers), response.text)
                probes.append({
                    "url": link["url"],
                    "declared_by": "official careers page anchor",
                    "anchor_text": link["text"],
                    "host": link["host"],
                    "kind": link["kind"],
                    "status_code": response.status_code,
                    "final_url": str(response.url),
                    "bytes": len(response.content),
                    "sha256": sha256(response.content).hexdigest(),
                    **{key: value for key, value in classification.items()
                       if key != "status_code"},
                    "duration_seconds": round(max(0.0, time.perf_counter() - started), 3),
                })
                if classification["state"] == "cloudflare_challenge":
                    # Escalate the exact declared destination through the stealth
                    # browser before treating it as blocked.
                    page = await access.fetch_with_stealth(link["url"])
                    probes[-1]["access"] = access.attempt_metadata(page, url=link["url"])
                    if page is not None and page.ok:
                        escalated = classify_destination(page.status or 200, {}, page.html)
                        probes[-1].update({
                            "state": escalated["state"],
                            "final_url": page.final_url,
                            "bytes": len(page.html.encode()),
                            "sha256": sha256(page.html.encode()).hexdigest(),
                            "resolved_by": "stealth_escalation",
                        })
                    else:
                        break
            except Exception as exc:
                probes.append({
                    "url": link["url"],
                    "declared_by": "official careers page anchor",
                    "anchor_text": link["text"],
                    "host": link["host"],
                    "kind": link["kind"],
                    "state": "request_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": round(max(0.0, time.perf_counter() - started), 3),
                })
        return probes

    def _finish_unavailable(self, source_started: float):
        seconds = max(0.0, time.perf_counter() - source_started)
        self.result.metadata["source_collection_wall_seconds"] = seconds
        self.result.timings["swym_source_collection_seconds"] = seconds
