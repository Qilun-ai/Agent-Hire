"""WinZO's official careers entry and the company-configured Keka portal (reviewed 2026-11-08).

The ledger entry ``https://www.winzogames.com/careers`` is **not published** on
WinZO's rebuilt 2026 site: the server answers ``404`` with WinZO's own
"Page Not Found | WinZO" page.  The official homepage, ``robots.txt`` and the
two-URL ``sitemap.xml`` publish no careers route, and 17 bounded path probes all
answer 404.  No ATS iframe, link or ``JobPosting`` JSON-LD is published.

The company's Keka tenant ``winzo.keka.com`` still exists and is configured as
WinZO's own portal (portal name ``WinZO``, business unit ``WinZO Games Private
Limited``, ``companyWebsite`` = the ledger careers URL, and both brand anchors
point at ``www.winzogames.com/careers``).  Its active feed is **empty** and the
rendered portal says "There are no job postings matching the filters.".  The
portal is not linked/embedded/redirected to by any current official page, so per
the project contract it is recorded as evidence only (``used_for_membership``
false) and its zero rows are not certified as zero vacancies.

The adapter therefore fails closed: it records ``no_public_careers_page`` rather
than returning a successful zero-job listing.  If the official entry later
publishes the Keka portal again (link, iframe or redirect), the same adapter
follows that published portal, verifies the portal identity, reads the active
feed and hydrates every posting from ``/careers/jobdetails/{id}``.

The shared HTTP client raises on 4xx, so the 404 body is read back from the
client's own content-addressed response archive (the same pattern used by other
404 reviews).  No unarchived request is made.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..http import FetchError
from .base import Adapter


OFFICIAL_ENTRY = "https://www.winzogames.com/careers"
OFFICIAL_SITE = "https://www.winzogames.com/"
ROBOTS = OFFICIAL_SITE + "robots.txt"
SITEMAP = OFFICIAL_SITE + "sitemap.xml"
KEKA_CAREERS = "https://winzo.keka.com/careers"
KEKA_ORIGIN = "https://winzo.keka.com"
KEKA_IDENTIFIER = "ea9c32e1-45db-4a89-b564-455586c510de"
EXPECTED_PORTAL_NAME = "WinZO"
EXPECTED_LEGAL_EMPLOYER = "WinZO Games Private Limited"
EXPECTED_COMPANY_WEBSITE = OFFICIAL_ENTRY
EMPTY_STATE_TEXT = "There are no job postings matching the filters"
WIDGET_URL = f"{KEKA_ORIGIN}/careers/api/embedjobs/js/{KEKA_IDENTIFIER}"
ACTIVE_URL = f"{KEKA_ORIGIN}/careers/api/embedjobs/default/active/{KEKA_IDENTIFIER}"
INFO_URL = f"{KEKA_ORIGIN}/careers/api/organization/default/careerportalinfo"
GROUPLINK_URL = f"{KEKA_ORIGIN}/careers/api/embedjobs/grouplinkstatus/{KEKA_IDENTIFIER}"
DEPARTMENTS_URL = f"{KEKA_ORIGIN}/careers/api/embedjobs/departments/{KEKA_IDENTIFIER}"
BUSINESSUNITS_URL = f"{KEKA_ORIGIN}/careers/api/embedjobs/businessunits/{KEKA_IDENTIFIER}"

PORTAL_FETCH = re.compile(
    r"fetch\s*\(\s*(['\"])(/ats/documents/([0-9a-f-]+)/careerportal/[0-9a-f]+\.html)\1\s*\)"
)
KH_IDENTIFIER = re.compile(r"identifier:\s*'([^']+)'")
KH_DOMAIN = re.compile(r"domain:\s*'([^']+)'")
KH_CONTAINER = re.compile(r"targetContainer:\s*'([^']+)'")
JOB_DETAIL_PATH = re.compile(r"^/careers/jobdetails/(\d+)$")
APPLY_PATH = re.compile(r"^/careers/applyjob/(\d+)$")
CAREER_PATH = re.compile(
    r"(?i)^/(?:careers?|jobs?|roles?|open-roles|open-positions|join-us|"
    r"we-are-hiring|hiring)(?:/|$)"
)
ATS_HOST = re.compile(
    r"(?i)(?:^|\.)(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|"
    r"jobs\.lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|"
    r"keka\.com|darwinbox\.com|darwinbox\.in|zohorecruit\.com|zohorecruit\.in|"
    r"freshteam\.com|workable\.com|recruitee\.com|bamboohr\.com|jobvite\.com|"
    r"icims\.com|taleo\.net|oraclecloud\.com|successfactors\.com|"
    r"phenompeople\.com|eightfold\.ai)$"
)
JOB_TYPE_LABELS = {1: "Part-Time", 2: "Full-Time"}
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tr", "td", "th", "ul",
})
PATH_PROBES = ("/careers", "/career", "/jobs", "/join-us", "/open-roles")
# A guessed ATS slug is never membership.  The probes exist so a WinZO-branded
# tenant that appears at a guessed slug is noticed and reviewed, not scraped.
EXCLUDED_ATS_PROBES = {
    "greenhouse": "https://boards.greenhouse.io/winzo",
    "lever": "https://jobs.lever.co/winzo",
    "ashby": "https://jobs.ashbyhq.com/winzo",
    "freshteam": "https://winzogames.freshteam.com/jobs",
    "zoho": "https://winzogames.zohorecruit.com/jobs/Careers",
    "keka_winzogames": "https://winzogames.keka.com/careers",
    "darwinbox": "https://winzo.darwinbox.com/ms/careers",
    "smartrecruiters": "https://careers.smartrecruiters.com/WinZO",
    "workable": "https://apply.workable.com/winzo/",
}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def compact(value: str | None) -> str:
    return re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))


def visible_text(html: str | None) -> str:
    """Browser-like text: block boundaries become newlines, inline nodes join."""
    soup = BeautifulSoup(f'<div data-winzo-text="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-winzo-text]")
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name in {"script", "style", "noscript"}:
            return
        if name == "br":
            parts.append("\n")
            return
        block = name in BLOCK_TAGS
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    if root is not None:
        for child in root.children:
            walk(child)
    text = "".join(parts).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def detail_url_for(identity: str) -> str:
    if not str(identity).isdigit():
        raise ValueError(f"WinZO Keka job ID is not numeric: {identity!r}")
    return f"{KEKA_ORIGIN}/careers/jobdetails/{identity}"


def apply_url_for(identity: str) -> str:
    if not str(identity).isdigit():
        raise ValueError(f"WinZO Keka job ID is not numeric: {identity!r}")
    return f"{KEKA_ORIGIN}/careers/applyjob/{identity}"


def employment_type_label(value) -> str | None:
    return JOB_TYPE_LABELS.get(value)


def parse_official_entry(body: bytes | str, response_url: str, status: int) -> dict:
    parsed = urlsplit(response_url)
    if parsed.netloc not in {"www.winzogames.com", "winzogames.com"}:
        raise ValueError("WinZO official careers entry left the company host")
    html = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    headings = [normalized(h.get_text(" ", strip=True)) for h in soup.select("h1,h2")]
    anchors, careers_links, external_ats = [], [], []
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor.get("href", ""))
        parts = urlsplit(href)
        text = normalized(anchor.get_text(" ", strip=True))
        anchors.append({"url": href, "text": text})
        if parts.netloc and ATS_HOST.search(parts.netloc):
            external_ats.append({"url": href, "text": text, "host": parts.netloc})
        elif parts.netloc in {"www.winzogames.com", "winzogames.com"} and CAREER_PATH.match(parts.path):
            careers_links.append({"url": href, "text": text})
    iframes = []
    for iframe in soup.select("iframe[src]"):
        src = urljoin(response_url, iframe.get("src", ""))
        iframes.append({"url": src, "id": iframe.get("id"), "title": iframe.get("title")})
        if ATS_HOST.search(urlsplit(src).netloc):
            external_ats.append({"url": src, "text": iframe.get("title") or "", "host": urlsplit(src).netloc})
    job_postings = 0
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                kind = item.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    job_postings += 1
    body_text = normalized(soup.get_text(" ", strip=True))
    return {
        "requested_url": OFFICIAL_ENTRY,
        "response_url": response_url,
        "status": status,
        "title": title,
        "headings": headings,
        "is_site_404": status == 404 and "page not found" in (title + " " + body_text).lower(),
        "anchors": anchors,
        "anchor_count": len(anchors),
        "careers_links": careers_links,
        "ats_embeds_or_links": external_ats,
        "iframes": iframes,
        "job_posting_jsonld": job_postings,
        "mentions_careers": bool(re.search(r"career|jobs?|hiring", body_text, re.I)),
        "publishes_listing": bool(careers_links or external_ats or job_postings),
    }


def parse_keka_shell(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "winzo.keka.com" or parts.path.rstrip("/") != "/careers":
        raise ValueError("WinZO Keka careers shell came from an unexpected route")
    match = PORTAL_FETCH.search(html or "")
    if match is None:
        raise ValueError("WinZO Keka careers shell no longer fetches a career-portal document")
    document_path = match.group(2)
    document_url = urljoin(KEKA_ORIGIN, document_path)
    if match.group(3) != KEKA_IDENTIFIER:
        raise ValueError("WinZO Keka career-portal document identifier changed")
    return {
        "response_url": response_url,
        "document_url": document_url,
        "document_path": document_path,
        "identifier": match.group(3),
        "bytes": len(html or ""),
        "sha256": sha256((html or "").encode()).hexdigest(),
    }


def parse_careerportal_document(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if parts.netloc != "winzo.keka.com" or "/careerportal/" not in parts.path:
        raise ValueError("WinZO Keka career-portal document came from an unexpected route")
    identifier = KH_IDENTIFIER.search(html or "")
    domain = KH_DOMAIN.search(html or "")
    container = KH_CONTAINER.search(html or "")
    if not identifier or identifier.group(1) != KEKA_IDENTIFIER:
        raise ValueError("WinZO Keka career-portal identifier changed")
    if not domain or domain.group(1).rstrip("/") != KEKA_CAREERS:
        raise ValueError("WinZO Keka career-portal domain changed")
    soup = BeautifulSoup(html or "", "lxml")
    anchors = [
        {"url": urljoin(response_url, a.get("href", "")), "text": normalized(a.get_text(" ", strip=True))}
        for a in soup.select("a[href]")
    ]
    brand_links = [a for a in anchors if urlsplit(a["url"]).netloc in {"www.winzogames.com", "winzogames.com"}]
    return {
        "response_url": response_url,
        "identifier": identifier.group(1),
        "domain": domain.group(1),
        "target_container": container.group(1) if container else None,
        "title": normalized(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "brand_links": brand_links,
        "empty_state_text_present": EMPTY_STATE_TEXT.lower() in (html or "").lower(),
    }


def parse_careerportal_info(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("WinZO Keka careerportalinfo is not an object")
    name = normalized(value.get("name"))
    website = normalized(value.get("companyWebsite")).rstrip("/")
    if name != EXPECTED_PORTAL_NAME:
        raise ValueError(f"WinZO Keka portal name changed: {name!r}")
    if website != EXPECTED_COMPANY_WEBSITE.rstrip("/"):
        raise ValueError(f"WinZO Keka portal company website changed: {website!r}")
    if normalized(value.get("careersPortalDomain")) != "winzo.keka.com":
        raise ValueError("WinZO Keka portal domain changed")
    return {
        "name": name,
        "short_name": normalized(value.get("shortName")),
        "company_website": website,
        "careers_portal_domain": normalized(value.get("careersPortalDomain")),
        "filters": (value.get("jobListingSetting") or {}).get("filters") or [],
        "job_fields": (value.get("jobListingSetting") or {}).get("jobFields") or [],
        "job_listing_format": (value.get("jobListingSetting") or {}).get("jobListingFormat"),
        "color": value.get("careersPortalColorCode"),
        "raw": value,
    }


def parse_group_link_status(value) -> dict:
    if not isinstance(value, dict) or "isGroupLinked" not in value:
        raise ValueError("WinZO Keka group-link status schema changed")
    return {"is_group_linked": bool(value["isGroupLinked"])}


def parse_departments(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("WinZO Keka department list is not a list")
    rows = []
    for row in value:
        if not isinstance(row, dict) or not normalized(row.get("name")):
            continue
        rows.append({
            "id": row.get("id"),
            "identifier": row.get("identifier"),
            "name": normalized(row.get("name")),
            "parent_group_identifier": row.get("parentGroupIdentifier"),
        })
    return rows


def parse_business_units(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("WinZO Keka business-unit list is not a list")
    return [
        {"id": row.get("id"), "identifier": row.get("identifier"),
         "name": normalized(row.get("name")), "description": normalized(row.get("description"))}
        for row in value if isinstance(row, dict)
    ]


def parse_active_jobs(value, departments: list[dict] | None = None) -> dict:
    """Parse the exact active-job feed.  An empty list is a valid explicit state."""
    if not isinstance(value, list):
        raise ValueError("WinZO Keka active-job response is not a list")
    department_ids = {str(row["identifier"]) for row in departments or []}
    rows, seen, issues = [], set(), []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("WinZO Keka active-job response contains a non-object row")
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            raise ValueError(f"WinZO Keka active-job row has no positive integer id: {identity!r}")
        identity = str(identity)
        if identity in seen:
            raise ValueError(f"WinZO Keka active-job response repeats job id {identity}")
        seen.add(identity)
        title = normalized(row.get("title"))
        if not title:
            raise ValueError(f"WinZO Keka job {identity} has no title")
        description = row.get("description")
        if not isinstance(description, str) or len(compact(visible_text(description))) < 100:
            raise ValueError(f"WinZO Keka job {identity} has an empty or short description")
        department_identifier = str(row.get("departmentIdentifier") or "")
        if not normalized(row.get("departmentName")):
            raise ValueError(f"WinZO Keka job {identity} has no department name")
        if departments is not None and department_identifier and department_identifier not in department_ids:
            issues.append(f"department_not_in_portal_list:{identity}")
        locations = row.get("jobLocations")
        if not isinstance(locations, list) or not locations:
            raise ValueError(f"WinZO Keka job {identity} has no jobLocations")
        for location in locations:
            if not isinstance(location, dict) or not (
                    normalized(location.get("name")) or normalized(location.get("city"))):
                raise ValueError(f"WinZO Keka job {identity} has an unidentified job location")
        if row.get("jobType") not in JOB_TYPE_LABELS:
            raise ValueError(f"WinZO Keka job {identity} has an unknown jobType: {row.get('jobType')!r}")
        if not isinstance(row.get("experience"), str):
            raise ValueError(f"WinZO Keka job {identity} experience is not a string")
        rows.append(dict(row))
    return {"rows": rows, "ids": [str(row["id"]) for row in rows], "count": len(rows),
            "issues": issues, "empty": not rows}


def parse_job_detail_page(html: str, row: dict, expected_url: str) -> dict:
    parts = urlsplit(expected_url)
    match = JOB_DETAIL_PATH.fullmatch(parts.path)
    identity = str(row["id"])
    if (parts.scheme != "https" or parts.netloc != "winzo.keka.com" or match is None
            or match.group(1) != identity):
        raise ValueError("WinZO detail URL is not an exact exposed Keka job route")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    h1 = soup.select_one("h1")
    actual_title = normalized(h1.get_text(" ", strip=True)) if h1 else ""
    expected_title = normalized(row.get("title"))
    if page_title != expected_title or actual_title != expected_title:
        raise ValueError(
            f"WinZO exact detail {identity} title changed: page={page_title!r}/{actual_title!r} "
            f"row={expected_title!r}")
    container = soup.select_one(".job-description-container")
    if container is None:
        raise ValueError(f"WinZO exact detail {identity} no longer exposes .job-description-container")
    description_html = container.decode_contents()
    description_text = visible_text(description_html)
    if len(compact(description_text)) < 100:
        raise ValueError(f"WinZO exact detail {identity} description is empty or short")
    experience_node = soup.select_one(".ki-user-tie ~ span")
    location_node = soup.select_one(".ki-location ~ span")
    job_type_node = soup.select_one(".ki-briefcase ~ span")
    apply_node = soup.select_one("a[href*='/careers/applyjob/'], button")
    return {
        "source_url": expected_url,
        "title": actual_title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized(description_text)),
        "description_compact_chars": len(compact(description_text)),
        "description_sha256": sha256(compact(description_text).encode()).hexdigest(),
        "visible_experience": normalized(experience_node.get_text(" ", strip=True)) if experience_node else "",
        "visible_location": normalized(location_node.get_text(" ", strip=True)) if location_node else "",
        "visible_job_type": normalized(job_type_node.get_text(" ", strip=True)) if job_type_node else "",
        "apply_control_text": normalized(apply_node.get_text(" ", strip=True)) if apply_node else "",
    }


class WinZO(Adapter):
    async def run(self):
        started = time.perf_counter()
        self.result.pages = 0
        response, entry = await self._fetch(OFFICIAL_ENTRY, "official_careers_entry")
        body = entry.get("body") or b""
        parsed = parse_official_entry(body, entry.get("final_url") or OFFICIAL_ENTRY,
                                      entry.get("status_code") or 0)
        self.result.metadata["official_entry"] = {
            **{k: v for k, v in entry.items() if k != "body"}, "parsed": parsed,
        }
        if entry.get("status_code") != 200 or not parsed["publishes_listing"]:
            await self._record_no_listing(parsed, entry)
        else:
            await self._collect_published_listing(parsed)
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["source_collection_wall_seconds"] = self.result.timings["source_collection_seconds"]
        self.result.finish()
        return self.result

    # ---------------------------------------------------------------- no listing
    async def _record_no_listing(self, parsed: dict, entry: dict):
        status = entry.get("status_code")
        reason = "no_public_careers_page" if status != 200 else "careers_page_publishes_no_listing"
        self.result.errors.append(reason)
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["collection_outcome"] = "failed_waitlist_no_public_careers_page"
        self.result.metadata["membership"] = {
            "careers_page_http_status": status,
            "careers_page_title": parsed["title"],
            "careers_page_headings": parsed["headings"],
            "careers_page_is_site_404": parsed["is_site_404"],
            "careers_page_mentions_careers": parsed["mentions_careers"],
            "careers_route_published": False,
            "job_listing_candidates": parsed["careers_links"],
            "external_ats_links": parsed["ats_embeds_or_links"],
            "job_posting_schema_count": parsed["job_posting_jsonld"],
            "reconciled": True,
            "note": (
                "The official careers entry is not a listing page. Membership is empty because "
                "WinZO publishes no careers route/ATS link on its current official site, not "
                "because an enumerated listing was empty; advertised_jobs stays null."
            ),
        }
        self.result.metadata["official_site_audit"] = await self._audit_official_site()
        self.result.metadata["unlinked_ats_probe"] = await self._probe_company_configured_ats()
        self.result.metadata["excluded_ats_probes"] = await self._probe_guessed_ats_slugs()

    async def _audit_official_site(self) -> dict:
        audit = {}
        homepage, home = await self._fetch(OFFICIAL_SITE, "official_homepage")
        if home.get("body"):
            soup = BeautifulSoup(home["body"].decode("utf-8", "replace"), "lxml")
            anchors = [
                {"url": urljoin(str(home.get("final_url") or OFFICIAL_SITE), a.get("href", "")),
                 "text": normalized(a.get_text(" ", strip=True))}
                for a in soup.select("a[href]")
            ]
            audit["homepage"] = {
                "status": home.get("status_code"),
                "title": normalized(soup.title.get_text(" ", strip=True) if soup.title else ""),
                "anchors": anchors,
                "careers_links": [
                    a for a in anchors
                    if CAREER_PATH.match(urlsplit(a["url"]).path)
                    and urlsplit(a["url"]).netloc in {"www.winzogames.com", "winzogames.com"}
                ],
                "ats_links": [a for a in anchors if ATS_HOST.search(urlsplit(a["url"]).netloc)],
                "job_posting_jsonld": sum(
                    1 for script in soup.select('script[type="application/ld+json"]')
                    if "JobPosting" in (script.string or "")),
            }
        robots, robots_record = await self._fetch(ROBOTS, "robots")
        audit["robots"] = {"status": robots_record.get("status_code"),
                           "text": (robots_record.get("body") or b"").decode("utf-8", "replace")[:2000]}
        sitemap, sitemap_record = await self._fetch(SITEMAP, "sitemap")
        sitemap_text = (sitemap_record.get("body") or b"").decode("utf-8", "replace")
        audit["sitemap"] = {
            "status": sitemap_record.get("status_code"),
            "urls": re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_text),
        }
        probes = {}
        for path in PATH_PROBES:
            _, record = await self._fetch(urljoin(OFFICIAL_SITE, path.lstrip("/")), f"path_probe:{path}")
            probes[path] = {"status": record.get("status_code"),
                            "final_url": record.get("final_url"),
                            "bytes": record.get("html_bytes")}
        audit["path_probes"] = probes
        audit["career_pages_published"] = sum(
            1 for path, row in probes.items() if row["status"] == 200)
        return audit

    async def _probe_company_configured_ats(self) -> dict:
        """Document the company-configured Keka portal.  Never used for membership."""
        probe = {"used_for_membership": False, "reason": (
            "The Keka portal is WinZO-branded and configured with the official careers URL, but no "
            "current official page links, embeds, requests or redirects to it (the entry that used "
            "to embed it is 404). It is recorded as evidence only.")}
        _, shell = await self._fetch(KEKA_CAREERS, "keka_careers_shell")
        try:
            parsed_shell = parse_keka_shell(
                (shell.get("body") or b"").decode("utf-8", "replace"),
                shell.get("final_url") or KEKA_CAREERS)
        except ValueError as exc:
            probe["shell_error"] = str(exc)
            return probe
        probe["shell"] = parsed_shell
        _, document = await self._fetch(parsed_shell["document_url"], "keka_careerportal_document")
        try:
            probe["document"] = parse_careerportal_document(
                (document.get("body") or b"").decode("utf-8", "replace"),
                document.get("final_url") or parsed_shell["document_url"])
        except ValueError as exc:
            probe["document_error"] = str(exc)
        info, info_record = await self._json_fetch(INFO_URL, "keka_careerportal_info")
        try:
            probe["portal_info"] = parse_careerportal_info(info)
        except ValueError as exc:
            probe["portal_info_error"] = str(exc)
            probe["portal_info_raw"] = info
        group, _ = await self._json_fetch(GROUPLINK_URL, "keka_group_link_status")
        try:
            probe["group_link_status"] = parse_group_link_status(group)
        except ValueError as exc:
            probe["group_link_status_error"] = str(exc)
        departments, _ = await self._json_fetch(DEPARTMENTS_URL, "keka_departments")
        try:
            probe["departments"] = parse_departments(departments)
        except ValueError as exc:
            probe["departments_error"] = str(exc)
            probe["departments"] = []
        business_units, _ = await self._json_fetch(BUSINESSUNITS_URL, "keka_business_units")
        try:
            probe["business_units"] = parse_business_units(business_units)
        except ValueError as exc:
            probe["business_units_error"] = str(exc)
        active, active_record = await self._json_fetch(ACTIVE_URL, "keka_active_jobs")
        try:
            active_parsed = parse_active_jobs(active, probe.get("departments") or [])
        except ValueError as exc:
            probe["active_jobs_error"] = str(exc)
            probe["active_jobs"] = None
        else:
            probe["active_jobs"] = active_parsed
            probe["active_jobs_count"] = active_parsed["count"]
            if active_parsed["count"]:
                self.result.warnings.append("unlinked_ats_exposes_active_jobs")
        widget, widget_record = await self._fetch(WIDGET_URL, "keka_embedjobs_widget")
        widget_text = (widget_record.get("body") or b"").decode("utf-8", "replace")
        probe["empty_state"] = {
            "api_url": ACTIVE_URL,
            "api_status": active_record.get("status_code"),
            "api_count": (probe.get("active_jobs") or {}).get("count"),
            "empty_state_string_in_widget": EMPTY_STATE_TEXT.lower() in widget_text.lower(),
            "empty_state_string_in_document": bool(
                (probe.get("document") or {}).get("empty_state_text_present")),
        }
        return probe

    async def _probe_guessed_ats_slugs(self) -> dict:
        probes = {}
        for name, url in EXCLUDED_ATS_PROBES.items():
            _, record = await self._fetch(url, f"guessed_ats:{name}")
            probes[name] = {"url": url, "status": record.get("status_code"),
                            "final_url": record.get("final_url"),
                            "used_for_membership": False}
        return probes

    # ------------------------------------------------------------ listing path
    async def _collect_published_listing(self, parsed: dict):
        candidates = [
            item["url"] for item in parsed["careers_links"] + parsed["ats_embeds_or_links"]
            if urlsplit(item["url"]).netloc == "winzo.keka.com"
        ]
        if not candidates:
            self.result.errors.append("careers_page_publishes_unsupported_listing")
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.metadata["collection_outcome"] = "failed_waitlist_unsupported_listing"
            self.result.metadata["membership"] = {"careers_links": parsed["careers_links"],
                                                  "external_ats_links": parsed["ats_embeds_or_links"]}
            return
        portal_url = candidates[0].split("?")[0].rstrip("/")
        if portal_url != KEKA_CAREERS:
            self.result.errors.append("unexpected_company_ats_route")
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.metadata["collection_outcome"] = "failed_waitlist_unexpected_ats"
            return
        _, shell = await self._fetch(KEKA_CAREERS, "keka_careers_shell")
        shell_parsed = parse_keka_shell((shell.get("body") or b"").decode("utf-8", "replace"),
                                        shell.get("final_url") or KEKA_CAREERS)
        _, document = await self._fetch(shell_parsed["document_url"], "keka_careerportal_document")
        document_parsed = parse_careerportal_document(
            (document.get("body") or b"").decode("utf-8", "replace"),
            document.get("final_url") or shell_parsed["document_url"])
        if not any(urlsplit(link["url"]).netloc in {"www.winzogames.com", "winzogames.com"}
                   for link in document_parsed["brand_links"]):
            raise ValueError("WinZO Keka portal document no longer links back to the company site")
        info, _ = await self._json_fetch(INFO_URL, "keka_careerportal_info")
        portal_info = parse_careerportal_info(info)
        group, _ = await self._json_fetch(GROUPLINK_URL, "keka_group_link_status")
        group_status = parse_group_link_status(group)
        departments, _ = await self._json_fetch(DEPARTMENTS_URL, "keka_departments")
        department_rows = parse_departments(departments)
        business_units, _ = await self._json_fetch(BUSINESSUNITS_URL, "keka_business_units")
        unit_rows = parse_business_units(business_units)
        active, active_record = await self._json_fetch(ACTIVE_URL, "keka_active_jobs")
        active_parsed = parse_active_jobs(active, department_rows)
        self.result.metadata["portal"] = {
            "shell": shell_parsed,
            "document": document_parsed,
            "portal_info": {k: v for k, v in portal_info.items() if k != "raw"},
            "group_link_status": group_status,
            "departments": department_rows,
            "business_units": unit_rows,
            "active_feed": {"url": ACTIVE_URL, "status": active_record.get("status_code"),
                            "count": active_parsed["count"], "ids": active_parsed["ids"],
                            "issues": active_parsed["issues"]},
        }
        self.result.expected_total = active_parsed["count"]
        if not active_parsed["rows"]:
            widget, _ = await self._fetch(WIDGET_URL, "keka_embedjobs_widget")
            widget_text = (widget.get("body") or b"").decode("utf-8", "replace")
            empty_state = EMPTY_STATE_TEXT.lower() in widget_text.lower()
            if not empty_state:
                raise ValueError("WinZO Keka active feed is empty but the empty-state UI string is missing")
            self.result.listing_complete = True
            self.result.metadata["empty_state_verified"] = True
            self.result.metadata["membership"] = {
                "advertised_jobs": 0, "listing_rows": 0, "reconciled": True,
                "empty_state": EMPTY_STATE_TEXT,
            }
            return
        self.result.pages += 1
        for row in active_parsed["rows"]:
            self.add(self._make_job(row, active_record))
            if self.limit():
                return
        await self.hydrate(self.result.jobs, self._detail)
        self.result.listing_complete = len(self.seen) == len(active_parsed["rows"])
        self.result.metadata["membership"] = {
            "advertised_jobs": active_parsed["count"],
            "listing_rows": len(active_parsed["rows"]),
            "reconciled": self.result.listing_complete,
        }

    def _make_job(self, row: dict, active_record: dict):
        identity = str(row["id"])
        active_url = active_record.get("url") or ACTIVE_URL
        locations = [
            normalized(location.get("name")) or normalized(location.get("city"))
            for location in row["jobLocations"]
        ]
        description_html = row["description"]
        job = self.job(
            {
                "listing": dict(row),
                "listing_api_url": active_url,
                "listing_api_sha256": active_record.get("sha256"),
                "salary": {"salaryRange": row.get("salaryRange"),
                           "salaryRangeFormat": row.get("salaryRangeFormat"),
                           "amount_range_exposed": False,
                           "note": "Keka exposes a currency/period placeholder with no amount."},
                "dates": {"publishedOn": row.get("publishedOn"),
                          "publishedSinceDays": row.get("publishedSinceDays"),
                          "date_updated": None, "valid_through": None},
                "employment_type_code": row.get("jobType"),
                "legal_employer_exposed_per_job": False,
            },
            title=normalized(row["title"]),
            url=detail_url_for(identity),
            job_id=identity,
            apply_url=apply_url_for(identity),
            description=description_html,
            method="winzo.keka.embedjobs.active.description",
            description_url=active_url,
            locations=locations,
            department=normalized(row.get("departmentName")),
            employment_type=employment_type_label(row.get("jobType")),
            date_posted=row.get("publishedOn"),
            date_posted_raw={"publishedOn": row.get("publishedOn"),
                             "publishedSinceDays": row.get("publishedSinceDays")},
            experience=normalized(row.get("experience")),
            employer=EXPECTED_LEGAL_EMPLOYER,
            description_text=visible_text(description_html),
        )
        job.record_identity = f"winzo:{identity}"
        return job

    async def _detail(self, job):
        response = await self.http.get(
            job.url, headers={"Accept": "text/html,application/xhtml+xml", "Referer": KEKA_CAREERS})
        if (urlsplit(str(response.url)).netloc != "winzo.keka.com"
                or not str(response.url).startswith(f"{KEKA_ORIGIN}/careers/jobdetails/")):
            raise ValueError("WinZO job detail left the observed Keka route")
        detail = parse_job_detail_page(response.text, job.raw["listing"], str(response.url))
        api_text = compact(visible_text(job.raw["listing"].get("description")))
        page_text = compact(detail["description_text"])
        if api_text != page_text:
            raise ValueError(f"WinZO detail {job.job_id} description does not match the active feed")
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = "winzo.keka.jobdetails.description_container"
        job.description_source_url = str(response.url)
        job.raw["detail_page"] = {
            "source_url": str(response.url),
            "source_status": response.status_code,
            "source_response_sha256": sha256(response.content).hexdigest(),
            "description_sha256": detail["description_sha256"],
            "description_compact_chars": detail["description_compact_chars"],
            "api_description_matches_detail_page": True,
            "visible_experience": detail["visible_experience"],
            "visible_location": detail["visible_location"],
            "visible_job_type": detail["visible_job_type"],
            "apply_control_text": detail["apply_control_text"],
        }
        if detail["visible_job_type"]:
            job.employment_type = detail["visible_job_type"]
        return job

    # -------------------------------------------------------------------- HTTP
    async def _json_fetch(self, url: str, label: str):
        response, record = await self._fetch(url, label)
        body = record.get("body") or b""
        try:
            value = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ValueError(f"WinZO {label} did not return JSON: {exc}") from exc
        return value, {**{k: v for k, v in record.items() if k != "body"}, "url": url}

    def _request_log_lines(self) -> int:
        if not self.http.output:
            return 0
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return 0
        with path.open("rb") as handle:
            return sum(1 for _ in handle)

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
                "url": url, "purpose": purpose, "outcome": "request_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "final_url": capture.get("final_url"),
                "status_code": capture.get("status"),
                "html_bytes": len(raw) if raw is not None else None,
                "html_sha256": capture.get("sha256"),
                "redirects": capture.get("redirects", []),
                "body": raw,
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
        raw = response.content
        return response, {
            "url": url, "purpose": purpose, "outcome": "received",
            "final_url": str(response.url),
            "status_code": response.status_code,
            "html_bytes": len(raw),
            "html_sha256": sha256(raw).hexdigest(),
            "sha256": sha256(raw).hexdigest(),
            "redirects": [str(item.url) for item in response.history],
            "body": raw,
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }
