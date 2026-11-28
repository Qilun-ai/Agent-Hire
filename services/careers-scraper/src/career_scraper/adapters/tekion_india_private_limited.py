"""Tekion's official careers entry, its company job-listing page, and the
company-scoped Ashby posting board that page embeds.

The inspected journey is:

    https://tekion.com/careers
      -> /job-openings (See All Jobs)
      -> /pages/careers/careers-results.js
      -> https://api.ashbyhq.com/posting-api/job-board/tekion
      -> /job-openings/job?id=<ashby uuid>&country=...&city=...

The board token is the one shipped by Tekion's own pages.  Only that exact
company-scoped board is enumerated; the adapter never guesses or discovers an
Ashby tenant.  Descriptions are the full ``descriptionHtml`` returned inline by
that same board response (the detail component calls the same endpoint with
``includeCompensation=true``).  The detail route is retained as the public job
URL and its shell is checked for the exact inspected selectors.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://tekion.com/careers"
OFFICIAL_BASE = "https://tekion.com"
LISTING_URL = "https://tekion.com/job-openings"
ASSET_BASE = "https://d1684cvllpxlzt.cloudfront.net/public/dist"
CAREERS_COMPONENT_PATH = "/pages/careers/careers.js"
LISTING_COMPONENT_PATH = "/pages/careers/careers-results.js"
DETAIL_COMPONENT_PATH = "/pages/careers/job-openings.js"
BOARD_NAME = "tekion"
BOARD_HOST = "api.ashbyhq.com"
BOARD_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
BOARD_API_URL = f"{BOARD_API_BASE}/{BOARD_NAME}?includeCompensation=true"
JOB_HOST = "jobs.ashbyhq.com"
JOB_BOARD_PATH = "/Tekion"
DETAIL_PATH = "/job-openings/job"
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
DETAIL_ROUTE = re.compile(
    r"/job-openings/job\?id=\$\{([A-Za-z_$][\w$]*)\.id\}"
    r"&country=\$\{encodeURIComponent\(\1\.country\)\}"
    r"&city=\$\{encodeURIComponent\(\1\.city\)\}"
)
CARD_CLASS = "tekion-design-system--card-job"
URL_SAFE = "-_.!~*'()"
RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|key\s+roles?\s*(?:&|and)?|what\s+you.?ll\s+do|your\s+impact",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualification|requirement|skills|experience|desired|what\s+you.?ll\s+bring|about\s+you",
    re.I,
)
EMPLOYMENT_LABELS = {
    "FullTime": "Full-time",
    "PartTime": "Part-time",
    "Intern": "Internship",
    "Contract": "Contract",
    "Temporary": "Temporary",
}
WORKPLACE_LABELS = {"OnSite": "On-site", "Remote": "Remote", "Hybrid": "Hybrid"}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def description_text_from_html(html: str | None) -> str:
    """Visible body text in the same whitespace shape as the rendered page.

    ``get_text(' ')`` inserts spaces around inline links, which the rendered
    ``innerText`` does not (for example ``here .`` / ``@ tekion.com``).  Using a
    newline separator and then collapsing whitespace, punctuation spacing and
    the e-mail token reproduces the observed rendered text for the two sampled
    detail pages.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text("\n", strip=True)
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([,;:.!?])", r"\1", value)
    return re.sub(r"@\s+", "@", value)


def _assert_tekion_page(url: str, path_prefix: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"tekion.com", "www.tekion.com"}
            or not parsed.path.startswith(path_prefix)):
        raise ValueError(f"Tekion page left its official HTTPS route: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _assert_tekion_page(response_url, "/careers")
    parsed = urlsplit(response_url)
    if parsed.path.rstrip("/") != "/careers":
        raise ValueError("Tekion official entry redirected away from /careers")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Tekion Careers" not in title:
        raise ValueError("Tekion official careers title changed")
    links = [
        anchor for anchor in soup.select('a[href="/job-openings"]')
        if normalized(anchor.get_text(" ", strip=True)).lower() == "see all jobs"
    ]
    if len(links) != 1:
        raise ValueError("Tekion careers page no longer exposes one See All Jobs link to /job-openings")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script") if script.string)
    if f"TEKION.loadAsset({{ path: '{CAREERS_COMPONENT_PATH}'" not in scripts:
        raise ValueError("Tekion careers page no longer loads the inspected careers component")
    canonical = next(
        (link.get("href") for link in soup.find_all("link")
         if link.get("rel") and "canonical" in link.get("rel")),
        None,
    )
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical,
        "listing_url": urljoin(response_url, "/job-openings"),
        "listing_link_text": normalized(links[0].get_text(" ", strip=True)),
        "careers_component_path": CAREERS_COMPONENT_PATH,
    }


def parse_job_listings_page(html: str, response_url: str = LISTING_URL) -> dict:
    _assert_tekion_page(response_url, "/job-openings")
    parsed = urlsplit(response_url)
    if parsed.path.rstrip("/") != "/job-openings" or parsed.query:
        raise ValueError("Tekion job listing page redirected to an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    container = soup.select_one(".tekion-design-system--job-cards-container")
    if container is None:
        raise ValueError("Tekion job listing page no longer exposes the job card container")
    required_ids = {
        "btn-filter-location", "list-filter-location", "btn-filter-dept",
        "list-filter-dept", "job-search-input",
    }
    missing = sorted(required_ids - {node.get("id") for node in soup.find_all(id=True)})
    if missing:
        raise ValueError(f"Tekion job listing filters changed: missing {missing}")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script") if script.string)
    if f"TEKION.loadAsset({{ path: '{LISTING_COMPONENT_PATH}'" not in scripts:
        raise ValueError("Tekion job listing page no longer loads the inspected listing component")
    return {
        "url": response_url,
        "title": title,
        "listing_component_path": LISTING_COMPONENT_PATH,
        "static_card_count": len(soup.select(f".{CARD_CLASS}")),
        "filter_controls": {
            "location": "btn-filter-location",
            "department": "btn-filter-dept",
            "keyword_search": "job-search-input",
        },
        "pagination_controls": [
            normalized(node.get_text(" ", strip=True))
            for node in soup.select("a,button")
            if re.search(r"^(?:next|previous|load more|show more|page \d+)$",
                         normalized(node.get_text(" ", strip=True)), re.I)
        ],
    }


def _component_identity(script: str, asset_url: str) -> dict:
    parsed = urlsplit(asset_url)
    if parsed.scheme != "https" or parsed.hostname != "d1684cvllpxlzt.cloudfront.net":
        raise ValueError(f"Tekion careers component came from an unexpected host: {asset_url}")
    board = re.search(r'DEFAULT_JOB_BOARD="([A-Za-z0-9_-]+)"', script)
    if board is None:
        raise ValueError("Tekion component no longer declares its default job board")
    if board.group(1) != BOARD_NAME:
        raise ValueError(f"Tekion component board changed from {BOARD_NAME!r}: {board.group(1)!r}")
    if "https://api.ashbyhq.com/posting-api/job-board" not in script:
        raise ValueError("Tekion component no longer points at the inspected Ashby posting API")
    if "includeCompensation=" not in script or "isListed" not in script:
        raise ValueError("Tekion component no longer implements the inspected board request/filter")
    route = DETAIL_ROUTE.search(script)
    return {
        "asset_url": asset_url,
        "sha256": sha256(script.encode("utf-8")).hexdigest(),
        "board_name": board.group(1),
        "api_base": BOARD_API_BASE,
        "detail_route_template": route.group(0) if route else None,
    }


def parse_careers_component(script: str, asset_url: str) -> dict:
    identity = _component_identity(script, asset_url)
    if "Greenhouse" in script or "greenhouse" in script:
        raise ValueError("Tekion careers component unexpectedly uses a Greenhouse board")
    if "featuredRoles" not in script:
        raise ValueError("Tekion careers component no longer builds featured roles from the board")
    identity["kind"] = "official_careers_component"
    return identity


def parse_listing_component(script: str, asset_url: str) -> dict:
    identity = _component_identity(script, asset_url)
    if DETAIL_ROUTE.search(script) is None:
        raise ValueError("Tekion listing component no longer exposes the inspected job detail route")
    for token in (f'class="{CARD_CLASS}"', "job-cards-container", "No positions found."):
        if token not in script:
            raise ValueError(f"Tekion listing component changed: missing {token!r}")
    identity["kind"] = "job_listing_component"
    identity["cards_selector"] = f".{CARD_CLASS}"
    identity["detail_route_template"] = DETAIL_ROUTE.search(script).group(0)
    return identity


def parse_detail_component(script: str, asset_url: str) -> dict:
    identity = _component_identity(script, asset_url)
    required = (
        ".rich-text-content", "applyUrl", "descriptionHtml", "getJob",
        "job-openings/job",
    )
    missing = [token for token in required if token not in script]
    if missing:
        raise ValueError(f"Tekion detail component changed: missing {missing}")
    identity["kind"] = "job_detail_component"
    identity["description_selector"] = ".rich-text-content"
    return identity


def parse_board_payload(payload: dict, requested_url: str = BOARD_API_URL,
                        *, snapshot: str = "initial") -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"Tekion {snapshot} board response is not an object")
    if payload.get("apiVersion") not in (1, "1"):
        raise ValueError(f"Tekion {snapshot} board apiVersion changed: {payload.get('apiVersion')!r}")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError(f"Tekion {snapshot} board response has no jobs array")
    if not raw_jobs:
        raise ValueError(f"Tekion {snapshot} board response is empty")
    parsed_url = urlsplit(requested_url)
    if (parsed_url.scheme != "https" or parsed_url.hostname != BOARD_HOST
            or parsed_url.path != f"/posting-api/job-board/{BOARD_NAME}"
            or parse_qs(parsed_url.query) != {"includeCompensation": ["true"]}):
        raise ValueError(f"Tekion {snapshot} board request left the inspected company route: {requested_url}")

    rows, seen, issues, unlisted = [], set(), [], []
    visible_count = 0
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            issues.append(f"{snapshot}:row_not_object")
            continue
        identity = raw.get("id")
        if not isinstance(identity, str) or not UUID.fullmatch(identity):
            issues.append(f"{snapshot}:invalid_id:{identity!r}")
            continue
        if identity in seen:
            issues.append(f"{snapshot}:duplicate_id:{identity}")
            continue
        seen.add(identity)
        listed = raw.get("isListed")
        if listed is False:
            unlisted.append(identity)
            continue
        if listed is not True:
            issues.append(f"{snapshot}:missing_is_listed:{identity}")
            continue
        visible_count += 1
        if not normalized(raw.get("title")):
            issues.append(f"{snapshot}:missing_title:{identity}")
            continue
        expected_job = f"https://{JOB_HOST}{JOB_BOARD_PATH}/{identity}"
        if raw.get("jobUrl") != expected_job:
            issues.append(f"{snapshot}:unexpected_job_url:{identity}")
            continue
        if raw.get("applyUrl") not in {expected_job, expected_job + "/application"}:
            issues.append(f"{snapshot}:unexpected_apply_url:{identity}")
            continue
        if not isinstance(raw.get("descriptionHtml"), str) or not raw["descriptionHtml"].strip():
            issues.append(f"{snapshot}:empty_description_html:{identity}")
        if not isinstance(raw.get("descriptionPlain"), str) or not raw["descriptionPlain"].strip():
            issues.append(f"{snapshot}:empty_description_plain:{identity}")
        published = raw.get("publishedAt")
        if not isinstance(published, str) or not published.strip():
            issues.append(f"{snapshot}:missing_published_at:{identity}")
        else:
            try:
                datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                issues.append(f"{snapshot}:invalid_published_at:{identity}")
        address = raw.get("address")
        if not isinstance(address, dict) or not isinstance(address.get("postalAddress"), dict):
            issues.append(f"{snapshot}:missing_postal_address:{identity}")
        if not isinstance(raw.get("secondaryLocations"), list):
            issues.append(f"{snapshot}:missing_secondary_locations:{identity}")
        if not isinstance(raw.get("compensation"), dict):
            issues.append(f"{snapshot}:missing_compensation:{identity}")
        rows.append(raw)
    if len(rows) != visible_count:
        issues.append(f"{snapshot}:visible_row_count_mismatch:{visible_count - len(rows)}")
    return {
        "snapshot": snapshot,
        "requested_url": requested_url,
        "api_version": payload.get("apiVersion"),
        "raw_count": len(raw_jobs),
        "visible_count": visible_count,
        "rows": rows,
        "unlisted_ids": unlisted,
        "issues": issues,
    }


def _ui_location(row: dict) -> tuple[str, str]:
    """Mirror the inspected component's _parseLocation/card URL logic."""
    address = row.get("address") or {}
    postal = address.get("postalAddress") or {}
    country = (postal.get("addressCountry") or "other").lower()
    location = (row.get("location") or "").lower().strip()
    remote = row.get("isRemote") is True or row.get("workplaceType") == "Remote"
    if remote:
        city = "remote"
    elif location:
        city = re.sub(r"\s+(?:hq|headquarters|regional office|office)$", "", location)
        city = re.sub(r"^virtual\s*-\s*", "", city).strip()
        city = city or "other"
    else:
        city = "other"
    return country, city


def native_detail_url(row: dict, base: str = OFFICIAL_BASE) -> str:
    identity = row.get("id")
    if not isinstance(identity, str) or not UUID.fullmatch(identity):
        raise ValueError(f"Tekion job ID is not the inspected UUID shape: {identity!r}")
    country, city = _ui_location(row)
    return (
        f"{base}{DETAIL_PATH}?id={identity}"
        f"&country={quote(country, safe=URL_SAFE)}"
        f"&city={quote(city, safe=URL_SAFE)}"
    )


def parse_detail_page(html: str, response_url: str, job_id: str) -> dict:
    _assert_tekion_page(response_url, DETAIL_PATH)
    parsed = urlsplit(response_url)
    if parsed.path.rstrip("/") != DETAIL_PATH:
        raise ValueError("Tekion detail route changed from /job-openings/job")
    query = parse_qs(parsed.query)
    if query.get("id") != [job_id]:
        raise ValueError(f"Tekion detail route returned a different job ID: {response_url}")
    if UUID.fullmatch(job_id) is None:
        raise ValueError(f"Tekion detail route job ID is not a UUID: {job_id!r}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Job":
        raise ValueError(f"Tekion detail page title changed: {title!r}")
    selectors = {
        "description": ".rich-text-content",
        "title": ".s-title-stick-fix-title",
        "content": ".details-content-details-job",
    }
    missing = [name for name, selector in selectors.items() if soup.select_one(selector) is None]
    if missing:
        raise ValueError(f"Tekion detail page shell changed: missing {missing}")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script") if script.string)
    if f"TEKION.loadAsset({{ path: '{DETAIL_COMPONENT_PATH}'" not in scripts:
        raise ValueError("Tekion detail page no longer loads the inspected detail component")
    return {
        "url": response_url,
        "final_url": response_url,
        "job_id": job_id,
        "title": title,
        "selectors": selectors,
        "detail_component_path": DETAIL_COMPONENT_PATH,
    }


def _heading_nodes(soup: BeautifulSoup) -> list[Tag]:
    """Actual headings plus the standalone bold paragraphs Ashby posts use."""
    headings = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
        text = normalized(node.get_text(" ", strip=True))
        if not text:
            continue
        if node.name != "p":
            headings.append(node)
            continue
        if node.find_parent("li") or len(text) > 80:
            continue
        strong = node.find(["strong", "b"])
        if strong is None or normalized(strong.get_text(" ", strip=True)) != text:
            continue
        headings.append(node)
    return headings


def extract_role_sections(html: str) -> dict:
    """Retain named responsibility/qualification sections from the full body."""
    soup = BeautifulSoup(html or "", "lxml")
    headings = _heading_nodes(soup)
    heading_ids = {id(node) for node in headings}
    sections = []
    for heading in headings:
        title = normalized(heading.get_text(" ", strip=True))
        if not title:
            continue
        chunks = []
        for sibling in heading.next_siblings:
            if id(sibling) in heading_ids:
                break
            if isinstance(sibling, Tag):
                chunks.append(str(sibling))
        body_html = "".join(chunks)
        body_text = description_text_from_html(body_html)
        if body_text:
            sections.append({"heading": title, "text": body_text, "html": body_html})
    responsibilities = [item for item in sections if RESPONSIBILITY_HEADING.search(item["heading"])]
    qualifications = [item for item in sections if QUALIFICATION_HEADING.search(item["heading"])]
    return {
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
    }


def salary_value(compensation) -> dict | None:
    if not isinstance(compensation, dict):
        return None
    summary = compensation.get("compensationTierSummary")
    scrapeable = compensation.get("scrapeableCompensationSalarySummary")
    tiers = compensation.get("compensationTiers") or []
    components = compensation.get("summaryComponents") or []
    if not summary and not scrapeable and not tiers and not components:
        return None
    salary_components = [
        item for item in components
        if isinstance(item, dict) and item.get("compensationType") == "Salary"
    ]
    first = salary_components[0] if salary_components else {}
    return {
        "source_field": "compensation",
        "summary": summary,
        "scrapeable_summary": scrapeable,
        "currency": first.get("currencyCode"),
        "min_value": first.get("minValue"),
        "max_value": first.get("maxValue"),
        "interval": first.get("interval"),
        "tiers": tiers,
        "summary_components": components,
        "note": "Source compensation field; the detail page appends a generic US salary disclaimer outside the job body.",
    }


class TekionIndia(Adapter):
    """Follow only Tekion's own careers entry, listing component and board."""

    VERIFICATION_REVISION = "tekion-india.official-careers.ashby-board.1"

    async def _timed_get(self, url: str, *, json_response: bool = False, phase: str = ""):
        started = time.perf_counter()
        if json_response:
            value = await self.http.json(url)
        else:
            value = await self.http.get(url)
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.metadata.setdefault("source_requests", []).append(
            {"phase": phase, "url": url, "seconds": round(elapsed, 6)}
        )
        return value

    def _make_job(self, row: dict, *, api_url: str, listing_component_url: str,
                  detail_component_url: str, initial_row: dict | None = None) -> object:
        identity = row["id"]
        native_url = native_detail_url(row)
        description_html = row.get("descriptionHtml") or ""
        body_text = description_text_from_html(description_html)
        sections = extract_role_sections(description_html)
        salary = salary_value(row.get("compensation"))
        primary_location = {
            "location": row.get("location"),
            "workplace_type": row.get("workplaceType"),
            "workplace_label": WORKPLACE_LABELS.get(row.get("workplaceType"), row.get("workplaceType")),
            "is_remote": row.get("isRemote"),
            "address": row.get("address"),
        }
        secondary = []
        for item in row.get("secondaryLocations") or []:
            if isinstance(item, dict):
                secondary.append({"secondary": True, **item})
        raw = {
            "listing": row,
            "source_ui": {
                "board_name": BOARD_NAME,
                "board_api_url": api_url,
                "listing_page_url": LISTING_URL,
                "listing_component_url": listing_component_url,
                "detail_component_url": detail_component_url,
                "native_detail_url": native_url,
                "ashby_job_url": row.get("jobUrl"),
                "apply_url": row.get("applyUrl"),
                "employment_type_code": row.get("employmentType"),
                "employment_type_label": EMPLOYMENT_LABELS.get(
                    row.get("employmentType"), row.get("employmentType")),
                "workplace_type_code": row.get("workplaceType"),
                "workplace_type_label": WORKPLACE_LABELS.get(
                    row.get("workplaceType"), row.get("workplaceType")),
                "role_sections": sections,
                "salary": salary,
                "company_identity": {
                    "requested_company": self.company.name,
                    "site_footer": "Tekion Corp.",
                    "note": "The public board does not publish a separate per-role legal employer.",
                },
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=normalized(row.get("title")),
            url=native_url,
            job_id=identity,
            requisition_id=None,
            apply_url=row.get("applyUrl"),
            description=description_html,
            method="tekion.official_careers.ashby_board.descriptionHtml",
            description_url=api_url,
            locations=[primary_location, *secondary],
            department=row.get("department"),
            team=row.get("team"),
            employment_type=EMPLOYMENT_LABELS.get(row.get("employmentType"), row.get("employmentType")),
            workplace_type=WORKPLACE_LABELS.get(row.get("workplaceType"), row.get("workplaceType")),
            date_posted=row.get("publishedAt"),
            date_posted_raw=row.get("publishedAt"),
            date_updated=None,
            valid_through=None,
            salary=salary,
            qualifications=sections["qualifications"],
            responsibilities=sections["responsibilities"],
            employer="Tekion",
        )
        job.description_text = body_text
        job.record_identity = f"tekion-india:{identity}"
        return job

    async def run(self):
        started = time.perf_counter()
        entry_url = self.company.careers_url
        if entry_url.rstrip("/") != OFFICIAL_ENTRY:
            raise ValueError(f"Tekion dedicated flow must start from {OFFICIAL_ENTRY}")
        if not self.page_guard():
            return self.result

        entry = await self._timed_get(entry_url, phase="official_entry")
        entry_info = parse_official_entry(entry.text, str(entry.url))
        if entry_info["listing_url"] != LISTING_URL:
            raise ValueError("Tekion careers page no longer links to the inspected listing URL")

        careers_component_url = urljoin(ASSET_BASE + "/", CAREERS_COMPONENT_PATH.lstrip("/"))
        careers_response = await self._timed_get(careers_component_url, phase="careers_component")
        careers_info = parse_careers_component(careers_response.text, str(careers_response.url))

        listing = await self._timed_get(LISTING_URL, phase="job_listings_page")
        listing_info = parse_job_listings_page(listing.text, str(listing.url))

        listing_component_url = urljoin(ASSET_BASE + "/", LISTING_COMPONENT_PATH.lstrip("/"))
        listing_component = await self._timed_get(listing_component_url, phase="listing_component")
        listing_component_info = parse_listing_component(
            listing_component.text, str(listing_component.url))
        if listing_component_info["board_name"] != careers_info["board_name"]:
            raise ValueError("Tekion careers and job-listing components disagree on the board name")

        detail_component_url = urljoin(ASSET_BASE + "/", DETAIL_COMPONENT_PATH.lstrip("/"))
        detail_component = await self._timed_get(detail_component_url, phase="detail_component")
        detail_component_info = parse_detail_component(detail_component.text, str(detail_component.url))
        if detail_component_info["board_name"] != BOARD_NAME:
            raise ValueError("Tekion detail component no longer uses the inspected board")

        api_url = BOARD_API_URL
        initial_payload = await self._timed_get(api_url, json_response=True, phase="board_initial")
        initial = parse_board_payload(initial_payload, api_url, snapshot="initial")
        initial_rows = initial["rows"]
        initial_by_id = {row["id"]: row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.errors.append(
                "Tekion official board returned zero visible jobs; the native empty state requires independent review"
            )
            self.result.metadata.update({
                "official_entry": entry_info,
                "careers_component": careers_info,
                "listing_page": listing_info,
                "listing_component": listing_component_info,
                "detail_component": detail_component_info,
                "board_api_url": api_url,
                "source_requests": self.result.metadata.get("source_requests", []),
            })
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - started)
            return self.result

        parse_started = time.perf_counter()
        jobs = [
            self._make_job(
                row,
                api_url=api_url,
                listing_component_url=listing_component_url,
                detail_component_url=detail_component_url,
            )
            for row in initial_rows
        ]
        inline_parse_seconds = max(0.0, time.perf_counter() - parse_started)
        for job in jobs:
            self.add(job)
        if len(self.result.jobs) != len(initial_rows):
            self.result.errors.append("tekion_deduplicated_visible_rows")

        detail_shells = {}
        for label, row in (("first_source_order", initial_rows[0]),
                           ("last_source_order", initial_rows[-1])):
            url = native_detail_url(row)
            response = await self._timed_get(url, phase=f"detail_shell_{label}")
            page = parse_detail_page(response.text, str(response.url), row["id"])
            detail_shells[label] = {**page, "job_title": row["title"]}

        final_payload = await self._timed_get(api_url, json_response=True, phase="board_final")
        final = parse_board_payload(final_payload, api_url, snapshot="final")
        final_rows = final["rows"]
        final_by_id = {row["id"]: row for row in final_rows}
        initial_ids = [row["id"] for row in initial_rows]
        final_ids = [row["id"] for row in final_rows]
        new_ids = [identity for identity in final_ids if identity not in initial_by_id]
        removed_ids = [identity for identity in initial_ids if identity not in final_by_id]
        changed_ids = [
            identity for identity in initial_ids
            if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]
        ]
        ids_match = initial_ids == final_ids
        complete = ids_match and not initial["issues"] and not final["issues"] \
            and len(self.result.jobs) == initial["visible_count"]
        if not complete:
            self.result.errors.append("tekion_membership_or_row_reconciliation_incomplete")
        self.result.expected_total = initial["visible_count"]
        self.result.listing_complete = complete
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": entry_info,
            "careers_component": careers_info,
            "listing_page": listing_info,
            "listing_component": listing_component_info,
            "detail_component": detail_component_info,
            "board_api_url": api_url,
            "board_name": BOARD_NAME,
            "scope_note": (
                "Membership is the unfiltered company-scoped Ashby board embedded by "
                "Tekion's own /job-openings page. The board is global; no country or "
                "seniority filter was applied."
            ),
            "membership_reconciliation": {
                "initial_count": initial["raw_count"],
                "initial_visible_count": initial["visible_count"],
                "final_count": final["raw_count"],
                "final_visible_count": final["visible_count"],
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "initial_final_id_order_matches": ids_match,
                "initial_final_full_rows_match": not changed_ids and ids_match,
                "changed_ids": changed_ids,
                "new_ids": new_ids,
                "removed_ids": removed_ids,
                "unlisted_initial_ids": initial["unlisted_ids"],
                "unlisted_final_ids": final["unlisted_ids"],
                "initial_issues": initial["issues"],
                "final_issues": final["issues"],
                "pagination": "single unpaginated board response; the UI renders all cards with no pagination controls",
                "complete": complete,
            },
            "detail_shells": detail_shells,
            "inline_description_count": len(self.result.jobs),
            "inline_description_parse_seconds_sum": round(inline_parse_seconds, 6),
            "whole_record_detail_requests_per_job": 0,
            "detail_page_description_source": (
                "The detail component's getJob() fetches the same board endpoint with "
                "includeCompensation=true and renders descriptionHtml into .rich-text-content."
            ),
            "source_requests": self.result.metadata.get("source_requests", []),
        })
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.timings["inline_detail_parse_seconds_sum"] = round(inline_parse_seconds, 6)
        return self.result
