"""Micron Technology's own Eightfold PCSX careers site and its exact detail API.

The company careers journey is ``https://careers.micron.com/`` -> ``/careers``.
That page is an Eightfold PCSX shell whose published search config limits the
listing to Workday-sourced, externally posted positions outside China.  The
listing is read with the exact UI request (10 rows per page, ``sort_by=timestamp``
for the published "Latest" sort, no user filters) and each card's full body with the exact ``position_details``
request used by the same page.  No guessed Workday board or aggregator is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.micron.com/"
LISTING_URL = "https://careers.micron.com/careers"
PORTAL_ORIGIN = "https://careers.micron.com"
DOMAIN = "micron.com"
LISTING_API = PORTAL_ORIGIN + "/api/pcsx/search"
DETAIL_API = PORTAL_ORIGIN + "/api/pcsx/position_details"
ROBOTS_URL = PORTAL_ORIGIN + "/robots.txt"
SITEMAP_INDEX_URL = PORTAL_ORIGIN + "/careers/sitemap_index.xml?domain=" + DOMAIN
SITEMAP_URL = PORTAL_ORIGIN + "/careers/sitemap.xml?domain=" + DOMAIN
PAGE_SIZE = 10
SORT_BY = "timestamp"
BASE_POSITION_FQ = (
    "position.system_id:workday AND is_externally_posted:1 AND -location_country:CN"
)
EMPLOYER_NAME = "Micron Technology"
class SourceChallenge(RuntimeError):
    """The source returned a bot-protection challenge; stop bulk requests."""


JOB_PATH = re.compile(r"^/careers/job/(\d+)$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TOKEN = re.compile(
    r"^(?:job description(?: summary)?|position summary|key responsibilities|"
    r"responsibilities(?: and tasks)?|ai (?:relevant )?job responsibilities|"
    r"minimum qualifications|preferred qualifications|required qualifications|"
    r"qualifications(?: and experience)?|requirements|education(?: and experience)?|"
    r"experience|skills|about micron technology.*)$",
    re.I,
)
RESPONSIBILITY_TOKEN = re.compile(r"responsibilit|what you(?:'|’)ll do|your role", re.I)
QUALIFICATION_TOKEN = re.compile(
    r"qualif|requirement|what you(?:'|’)ll need|who you are|education|experience", re.I
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_plain_text(value: str | None) -> str:
    value = (value or "").replace("\xa0", " ")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace/punctuation normalisation used for body hashes and audits."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True).replace("\xa0", " ")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def epoch_iso(value) -> str | None:
    """Convert a source epoch-seconds field without inventing a missing date."""
    try:
        timestamp = float(value)
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_official_entry(html: str, response_url: str = LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "careers.micron.com"
        or parsed.path.rstrip("/") != "/careers"
    ):
        raise ValueError("Micron official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "micron" not in title.lower() or "career" not in title.lower():
        raise ValueError("Micron official careers page title changed")
    canonical = soup.select_one('link[rel="canonical"]')
    if not canonical or canonical.get("href") != LISTING_URL:
        raise ValueError("Micron official careers page canonical URL changed")
    config_node = soup.select_one("code#pcsx-data")
    if config_node is None:
        raise ValueError("Micron official careers page no longer exposes its PCSX config")
    try:
        config = json.loads(config_node.get_text())
    except (TypeError, ValueError) as exc:
        raise ValueError("Micron PCSX config is not valid JSON") from exc
    pcsx = (config.get("configs") or {}).get("pcsxConfig") or {}
    search = pcsx.get("searchConfig") or {}
    if config.get("domain") != DOMAIN:
        raise ValueError("Micron PCSX domain changed")
    if pcsx.get("enabled") is not True:
        raise ValueError("Micron PCSX listing is no longer enabled")
    if search.get("basePositionFq") != BASE_POSITION_FQ:
        raise ValueError("Micron PCSX membership query changed")
    if search.get("membershipFq") not in (None, ""):
        raise ValueError("Micron PCSX added an unexpected membership filter")
    if search.get("excludePrivatePositions") is not True:
        raise ValueError("Micron PCSX private-position exclusion changed")
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical.get("href"),
        "html_lang": (soup.html or {}).get("lang") if soup.html else None,
        "domain": config.get("domain"),
        "pcsx_enabled": bool(pcsx.get("enabled")),
        "base_position_fq": search.get("basePositionFq"),
        "membership_fq": search.get("membershipFq"),
        "exclude_private_positions": search.get("excludePrivatePositions"),
        "smart_filters": search.get("smartFilters") or [],
        "all_filters": search.get("allFilters") or [],
        "department_field": search.get("departmentField"),
    }


def parse_listing_page(value: dict, requested_start: int,
                       expected_count: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Micron PCSX listing response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Micron PCSX listing returned status {value.get('status')!r}")
    error = value.get("error") or {}
    if error.get("message"):
        raise ValueError(f"Micron PCSX listing error: {error.get('message')}")
    data = value.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
        raise ValueError("Micron PCSX listing response is missing data.positions")
    count = data.get("count")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Micron PCSX listing count is missing or invalid")
    if requested_start < 0 or requested_start % PAGE_SIZE:
        raise ValueError("Micron PCSX listing offset is not a UI page boundary")
    if expected_count is not None and count != expected_count:
        raise ValueError("Micron PCSX listing count changed during enumeration")
    if data.get("sortBy") != SORT_BY:
        raise ValueError("Micron PCSX listing sort changed")
    applied = data.get("appliedFilters")
    if applied not in (None, {}):
        raise ValueError(f"Micron PCSX listing unexpectedly applied filters: {applied!r}")

    rows, seen, issues = [], set(), []
    for row in data["positions"]:
        if not isinstance(row, dict):
            issues.append("micron_listing_row_not_object")
            continue
        identity = str(row.get("id", ""))
        if not re.fullmatch(r"\d+", identity) or int(identity) <= 0:
            issues.append("micron_listing_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"micron_listing_duplicate_id:{identity}")
            continue
        name = normalized_title(row.get("name"))
        display_id = str(row.get("displayJobId") or "")
        ats_id = str(row.get("atsJobId") or "")
        locations = row.get("locations")
        if not name or not display_id or not ats_id:
            issues.append(f"micron_listing_row_identity_fields_missing:{identity}")
            continue
        if display_id != ats_id:
            issues.append(f"micron_listing_row_requisition_mismatch:{identity}")
            continue
        if not isinstance(locations, list) or not locations or not all(
            isinstance(value, str) and value.strip() for value in locations
        ):
            issues.append(f"micron_listing_row_locations_missing:{identity}")
            continue
        if not isinstance(row.get("postedTs"), int) or row["postedTs"] <= 0:
            issues.append(f"micron_listing_row_posted_date_missing:{identity}")
            continue
        if not isinstance(row.get("creationTs"), int) or row["creationTs"] <= 0:
            issues.append(f"micron_listing_row_creation_date_missing:{identity}")
            continue
        if not isinstance(row.get("workLocationOption"), str) or not row["workLocationOption"].strip():
            issues.append(f"micron_listing_row_workplace_missing:{identity}")
            continue
        match = JOB_PATH.fullmatch(str(row.get("positionUrl") or ""))
        if match is None or match.group(1) != identity:
            issues.append(f"micron_listing_row_position_url_mismatch:{identity}")
            continue
        if not isinstance(row.get("standardizedLocations"), list):
            issues.append(f"micron_listing_row_standardized_locations_missing:{identity}")
        seen.add(identity)
        rows.append(dict(row))

    expected_rows = min(PAGE_SIZE, max(0, count - requested_start))
    if len(rows) != expected_rows:
        issues.append(
            f"micron_listing_page_row_count_mismatch:{requested_start}:"
            f"{len(rows)}!={expected_rows}"
        )
    pages = math.ceil(count / PAGE_SIZE) if count else 0
    return {
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "count": count,
        "page": requested_start // PAGE_SIZE + 1,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "requested_start": requested_start,
        "sort_by": data.get("sortBy"),
        "applied_filters": applied or {},
        "issues": issues,
    }


def parse_robots(text: str, response_url: str = ROBOTS_URL) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "careers.micron.com"
        or parsed.path != "/robots.txt"
    ):
        raise ValueError("Micron robots.txt redirected away from its inspected URL")
    sitemaps = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                sitemaps.append(value)
    if SITEMAP_INDEX_URL not in sitemaps:
        raise ValueError("Micron robots.txt no longer publishes the inspected sitemap index")
    return {"url": response_url, "sitemaps": sitemaps}


def parse_sitemap_index(xml: str, response_url: str = SITEMAP_INDEX_URL) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "careers.micron.com"
        or parsed.path != "/careers/sitemap_index.xml"
    ):
        raise ValueError("Micron sitemap index redirected away from its inspected URL")
    soup = BeautifulSoup(xml, "xml")
    locations = [node.get_text(strip=True) for node in soup.find_all("loc")]
    if SITEMAP_URL not in locations:
        raise ValueError("Micron sitemap index no longer lists the job sitemap")
    for location in locations:
        parts = urlsplit(location)
        if parts.scheme != "https" or parts.netloc != "careers.micron.com":
            raise ValueError(f"Micron sitemap index contains a foreign URL: {location}")
    return {"url": response_url, "sitemaps": locations, "job_sitemap_url": SITEMAP_URL}


def parse_job_sitemap(xml: str, response_url: str = SITEMAP_URL,
                      advertised_count: int | None = None) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "careers.micron.com"
        or parsed.path != "/careers/sitemap.xml"
    ):
        raise ValueError("Micron job sitemap redirected away from its inspected URL")
    soup = BeautifulSoup(xml, "xml")
    rows, seen, issues = [], set(), []
    for node in soup.find_all("url"):
        location_node = node.find("loc")
        location = location_node.get_text(strip=True) if location_node else ""
        parts = urlsplit(location)
        if not location:
            issues.append("micron_sitemap_url_missing_loc")
            continue
        if parts.scheme != "https" or parts.netloc != "careers.micron.com":
            issues.append(f"micron_sitemap_foreign_url:{location}")
            continue
        if not parts.path.startswith("/careers/job/"):
            continue
        query = parse_qs(parts.query)
        if query != {"domain": [DOMAIN]}:
            issues.append(f"micron_sitemap_job_query_changed:{location}")
            continue
        match = re.fullmatch(r"/careers/job/(\d+)(?:-[^/]*)?", parts.path)
        if match is None:
            issues.append(f"micron_sitemap_job_path_changed:{location}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"micron_sitemap_duplicate_id:{identity}")
            continue
        lastmod_node = node.find("lastmod")
        lastmod = lastmod_node.get_text(strip=True) if lastmod_node else None
        seen.add(identity)
        rows.append({"id": identity, "url": location, "lastmod": lastmod})
    if not rows:
        issues.append("micron_sitemap_has_no_job_urls")
    if advertised_count is not None and len(rows) < advertised_count:
        issues.append(
            f"micron_sitemap_smaller_than_listing:{len(rows)}<{advertised_count}"
        )
    lastmods = sorted(row["lastmod"] for row in rows if row["lastmod"])
    return {
        "url": response_url,
        "rows": rows,
        "ids": [row["id"] for row in rows],
        "count": len(rows),
        "lastmod_min": lastmods[0] if lastmods else None,
        "lastmod_max": lastmods[-1] if lastmods else None,
        "issues": issues,
    }


def detail_membership(data: dict, identity: str | None = None) -> dict:
    """Whether an exact detail row is the publicly posted listing membership.

    Sitemap extras can still return HTTP 200 with a body, but they publish no
    posting timestamp and no canonical ``publicUrl``.  Such rows are closed
    drift, not posted jobs, and must never be counted as descriptions.
    """
    identity = str(identity if identity is not None else data.get("id") or "")
    posted = data.get("postedTs")
    canonical = f"{PORTAL_ORIGIN}/careers/job/{identity}"
    public_url = data.get("publicUrl")
    is_posted = bool(
        identity
        and isinstance(posted, (int, float))
        and posted > 0
        and public_url == canonical
        and data.get("positionUrl") == f"/careers/job/{identity}"
    )
    return {
        "posted": is_posted,
        "posted_ts": posted,
        "public_url": public_url,
        "position_url": data.get("positionUrl"),
        "canonical_url": canonical,
    }


def parse_detail(value: dict, expected_row: dict | None = None,
                 expected_id: str | None = None,
                 allow_unlisted: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Micron PCSX detail response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Micron PCSX detail returned status {value.get('status')!r}")
    error = value.get("error") or {}
    if error.get("message"):
        raise ValueError(f"Micron PCSX detail error: {error.get('message')}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Micron PCSX detail response is missing data")
    identity = str(expected_id if expected_id is not None
                   else (expected_row or {}).get("id") or "")
    if not identity or str(data.get("id")) != identity:
        raise ValueError("Micron PCSX detail returned a different position id")
    actual_title = normalized_title(data.get("name"))
    if not actual_title:
        raise ValueError(f"Micron PCSX detail has no title for {identity}")
    if expected_row:
        expected_title = normalized_title(expected_row.get("name"))
        if expected_title and actual_title != expected_title:
            raise ValueError(
                f"Micron PCSX detail title changed for {identity}: "
                f"{actual_title!r} != {expected_title!r}"
            )
        for key in ("displayJobId", "atsJobId"):
            expected_value = str(expected_row.get(key) or "")
            if expected_value and str(data.get(key) or "") != expected_value:
                raise ValueError(f"Micron PCSX detail {key} changed for {identity}")
    membership = detail_membership(data, identity)
    posted = membership["posted"]
    public_url = membership["public_url"]
    public_url_missing = public_url in (None, "")
    if public_url is not None and not public_url_missing and public_url != membership["canonical_url"]:
        raise ValueError(f"Micron PCSX detail publicUrl changed for {identity}")
    if public_url_missing and not allow_unlisted:
        raise ValueError(f"Micron PCSX detail publicUrl changed for {identity}")
    if data.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"Micron PCSX detail positionUrl changed for {identity}")
    description = data.get("jobDescription")
    if not isinstance(description, str) or not description.strip():
        if posted or not allow_unlisted:
            raise ValueError(f"Micron PCSX detail has no jobDescription for {identity}")
    if not isinstance(data.get("locations"), list) or not data["locations"]:
        if posted or not allow_unlisted:
            raise ValueError(f"Micron PCSX detail has no locations for {identity}")
    issues = []
    if public_url_missing:
        issues.append(f"micron_detail_public_url_missing:{identity}")
    if expected_row:
        if expected_row.get("locations") and (
            list(data.get("locations") or []) != list(expected_row.get("locations") or [])
        ):
            issues.append(f"micron_detail_locations_changed:{identity}")
        if (
            expected_row.get("postedTs") is not None
            and data.get("postedTs") != expected_row.get("postedTs")
        ):
            issues.append(f"micron_detail_posted_ts_changed:{identity}")
    if not isinstance(data.get("postedTs"), (int, float)) or data["postedTs"] <= 0:
        issues.append(f"micron_detail_missing_posted_ts:{identity}")
    apply_status = (
        (data.get("positionUserActions") or {}).get("applyAction") or {}
    ).get("status")
    if apply_status != "allowed":
        issues.append(f"micron_detail_apply_status_{apply_status}:{identity}")
    return {
        "row": data,
        "issues": issues,
        "apply_status": apply_status,
        "posted": posted,
        "membership": membership,
    }


def parse_detail_page_jsonld(html: str, expected_id: str | None = None) -> dict:
    """Read the public JobPosting JSON-LD embedded in an exact detail page."""
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.get_text())
        except ValueError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                postings.append(candidate)
    if len(postings) != 1:
        raise ValueError("Micron detail page no longer exposes exactly one JobPosting JSON-LD")
    posting = postings[0]
    if expected_id and f"/careers/job/{expected_id}" not in str(posting.get("url") or ""):
        raise ValueError("Micron detail page JSON-LD identifies a different posting")
    if not isinstance(posting.get("description"), str) or not posting["description"].strip():
        raise ValueError("Micron detail page JSON-LD has no description")
    return {
        "title": normalized_title(posting.get("title")),
        "url": posting.get("url"),
        "date_posted": posting.get("datePosted"),
        "valid_through": posting.get("validThrough"),
        "employment_type": posting.get("employmentType"),
        "hiring_organization": posting.get("hiringOrganization"),
        "language": posting.get("inLanguage"),
        "description_chars": len(posting["description"]),
        "description_html": posting["description"],
        "description_text": normalized_plain_text(posting["description"]),
    }


def description_sections(html: str) -> list[dict]:
    """Keep employer-authored headings and the exact body below each one."""
    soup = BeautifulSoup(f'<div data-micron-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-micron-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong = block.find(["strong", "b"])
            strong_text = (
                normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            )
            if (
                strong_text
                and len(full_text) <= 120
                and (
                    strong_text == full_text
                    or full_text == f"{strong_text}:"
                    or HEADING_TOKEN.fullmatch(strong_text.rstrip(":"))
                )
            ):
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    return sections


def structured_sections(body: str) -> list[dict]:
    """Split the retained body text at observed Micron section headings."""
    sections, current = [], None
    for line in (body or "").splitlines():
        value = normalized_title(line)
        if HEADING_TOKEN.fullmatch(value.rstrip(":")):
            if current is not None:
                sections.append(current)
            current = {"heading": value, "text": ""}
        elif current is not None:
            current["text"] += ("\n" if current["text"] else "") + line.strip()
    if current is not None:
        sections.append(current)
    return [section for section in sections if section["text"].strip()]


def role_sections(sections: list[dict], pattern: re.Pattern) -> list[dict]:
    selected = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section.get("heading") and pattern.search(section["heading"])
    ]
    return selected or None


def description_links(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    return [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in soup.select("a[href]")
    ]


class MicronTechnology(Adapter):
    """Collect Micron's company-published Eightfold PCSX careers membership.

    The visible listing advertises 2878 jobs and paginates 10 per page, but its
    Company-priority ranking reshuffles between page requests.  The official
    robots.txt publishes a stable job sitemap (2885 URLs); every URL is checked
    against the exact same PCSX detail API.  Current roles have a positive
    ``postedTs`` and match the advertised listing count; stale sitemap entries
    are 404 or have no posting date and are recorded, not counted as jobs.
    """

    VERIFICATION_REVISION = "micron.pcsx-sitemap-company-careers.2"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html"})
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            redirect_chain=[str(history.url) for history in response.history],
        )
        return entry

    async def _listing_page(self, start, expected_count=None):
        if not self.page_guard():
            raise ValueError("Micron listing page budget exhausted")
        request_started = time.perf_counter()
        response = await self.http.request(
            "GET",
            LISTING_API,
            params={
                "domain": DOMAIN,
                "query": "",
                "location": "",
                "start": start,
                "sort_by": SORT_BY,
            },
            headers={"Accept": "application/json", "Referer": LISTING_URL},
        )
        request_seconds = max(0.0, time.perf_counter() - request_started)
        parsed_url = urlsplit(str(response.url))
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "careers.micron.com"
            or parsed_url.path != "/api/pcsx/search"
        ):
            raise ValueError("Micron listing API redirected outside its company route")
        parsed = parse_listing_page(response.json(), start, expected_count)
        parsed.update(
            request_url=str(response.request.url),
            final_url=str(response.url),
            request_seconds=request_seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _listing_sample(self):
        """Read the first, second and last rendered listing pages."""
        started = time.perf_counter()
        first = await self._listing_page(0)
        count, pages = first["count"], first["pages"]
        if self.result.expected_total is None:
            self.result.expected_total = count
        samples, starts = [first], {0}
        if count > PAGE_SIZE:
            second_start = PAGE_SIZE
            samples.append(await self._listing_page(second_start, count))
            starts.add(second_start)
        last_start = max(0, (pages - 1) * PAGE_SIZE)
        if last_start not in starts:
            samples.append(await self._listing_page(last_start, count))
        issues = []
        for sample in samples:
            issues.extend(sample["issues"])
        sample_ids = [str(row["id"]) for sample in samples for row in sample["rows"]]
        return {
            "count": count,
            "pages": pages,
            "sample_ids": sample_ids,
            "samples": [
                {
                    "page": sample["page"],
                    "start": sample["requested_start"],
                    "count": sample["count"],
                    "pages": sample["pages"],
                    "job_ids": sample["job_ids"],
                    "request_url": sample["request_url"],
                    "final_url": sample["final_url"],
                    "request_seconds": sample["request_seconds"],
                    "response_bytes": sample["response_bytes"],
                    "response_sha256": sample["response_sha256"],
                }
                for sample in samples
            ],
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _sitemap_snapshot(self, advertised_count=None):
        started = time.perf_counter()
        robots_response = await self.http.get(
            ROBOTS_URL, headers={"Accept": "text/plain"}
        )
        robots = parse_robots(robots_response.text, str(robots_response.url))
        index_response = await self.http.get(
            SITEMAP_INDEX_URL, headers={"Accept": "application/xml"}
        )
        index = parse_sitemap_index(index_response.text, str(index_response.url))
        sitemap_response = await self.http.get(
            index["job_sitemap_url"], headers={"Accept": "application/xml"}
        )
        parsed = parse_job_sitemap(
            sitemap_response.text,
            str(sitemap_response.url),
            advertised_count=advertised_count,
        )
        parsed.update(
            robots_url=ROBOTS_URL,
            robots_sitemaps=robots["sitemaps"],
            robots_sha256=sha256(robots_response.content).hexdigest(),
            sitemap_index_url=SITEMAP_INDEX_URL,
            sitemap_index_sha256=sha256(index_response.content).hexdigest(),
            sitemap_url=str(sitemap_response.url),
            sitemap_sha256=sha256(sitemap_response.content).hexdigest(),
            sitemap_bytes=len(sitemap_response.content),
            seconds=max(0.0, time.perf_counter() - started),
        )
        return parsed

    def _add_sitemap_placeholders(self, rows):
        for row in rows:
            identity = str(row["id"])
            job = self.job(
                {"sitemap": dict(row)},
                title="",
                url=f"{PORTAL_ORIGIN}/careers/job/{identity}",
                job_id=identity,
                requisition_id=None,
                apply_url=f"{PORTAL_ORIGIN}/careers/apply?pid={identity}",
                locations=[],
                department=None,
                employment_type=None,
                workplace_type=None,
                date_posted=None,
                date_posted_raw=None,
                date_updated=None,
                valid_through=None,
                salary=None,
                employer=EMPLOYER_NAME,
                language="en",
            )
            job.raw["sitemap"] = dict(row)
            job.raw["listing"] = {
                "id": int(identity),
                "source": "sitemap",
                "sitemap_url": row["url"],
                "sitemap_lastmod": row.get("lastmod"),
            }
            self.add(job)

    async def _detail(self, job):
        if self.challenge:
            raise SourceChallenge(self.challenge)
        params = {"position_id": job.job_id, "domain": DOMAIN, "hl": "en"}
        try:
            response = await self.http.request(
                "GET",
                DETAIL_API,
                params=params,
                headers={"Accept": "application/json", "Referer": job.url},
            )
        except FetchError as exc:
            if "HTTP 405" in str(exc):
                self.challenge = (
                    "micron_detail_waf_challenge: HTTP 405 AWS WAF Human Verification "
                    f"challenge at {exc}; bulk detail collection stopped and the "
                    "challenge was not solved or bypassed"
                )
                raise SourceChallenge(self.challenge) from exc
            raise
        parsed_url = urlsplit(str(response.url))
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "careers.micron.com"
            or parsed_url.path != "/api/pcsx/position_details"
        ):
            raise ValueError("Micron detail API redirected outside its company route")
        parsed = parse_detail(
            response.json(), expected_id=job.job_id, allow_unlisted=True
        )
        row = parsed["row"]
        if not parsed["posted"]:
            job.title = normalized_title(row.get("name"))
            job.department = row.get("department")
            job.workplace_type = row.get("workLocationOption")
            job.requisition_id = str(row.get("displayJobId") or "") or None
            job.date_posted = None
            job.date_posted_raw = row.get("postedTs")
            job.employer = EMPLOYER_NAME
            job.language = "en"
            job.warnings.append("not_in_posted_listing_membership")
            job.raw["detail"] = {
                "row": row,
                "posted": False,
                "membership": parsed["membership"],
                "issues": parsed["issues"],
                "description_not_retained": True,
            }
            job.raw["date_fields"] = {
                "postedTs": row.get("postedTs"),
                "creationTs": row.get("creationTs"),
                "postedTs_mapping": None,
                "creationTs_mapping": None,
            }
            return job
        description = row["jobDescription"]
        sections = description_sections(description)
        links = description_links(description)
        text_sections = structured_sections(text_html(description))
        normalized = normalized_body_text(description)
        if len(normalized) < 100:
            job.description_unavailable = True
            job.warnings.append("source_description_empty_or_truncated")
        job.title = normalized_title(row.get("name"))
        job.url = row["publicUrl"]
        job.locations = list(row.get("locations") or [])
        job.department = row.get("department")
        job.workplace_type = row.get("workLocationOption")
        job.requisition_id = str(row.get("displayJobId") or "") or None
        job.date_posted = epoch_iso(row.get("postedTs"))
        job.date_posted_raw = row.get("postedTs")
        job.apply_url = f"{PORTAL_ORIGIN}/careers/apply?pid={job.job_id}"
        job.employer = EMPLOYER_NAME
        job.language = "en"
        job.description_html = description
        job.description_text = text_html(description)
        job.description_method = "micron.pcsx.position_details.jobDescription"
        job.description_source_url = (
            f"{DETAIL_API}?position_id={job.job_id}&domain={DOMAIN}&hl=en"
        )
        job.responsibilities = role_sections(text_sections, RESPONSIBILITY_TOKEN)
        job.qualifications = role_sections(text_sections, QUALIFICATION_TOKEN)
        listing_row = {
            "id": row.get("id"),
            "displayJobId": row.get("displayJobId"),
            "name": row.get("name"),
            "locations": row.get("locations"),
            "standardizedLocations": row.get("standardizedLocations"),
            "postedTs": row.get("postedTs"),
            "creationTs": row.get("creationTs"),
            "department": row.get("department"),
            "workLocationOption": row.get("workLocationOption"),
            "locationFlexibility": row.get("locationFlexibility"),
            "atsJobId": row.get("atsJobId"),
            "positionUrl": row.get("positionUrl"),
            "source": "sitemap_detail",
            "sitemap_url": (job.raw.get("sitemap") or {}).get("url"),
            "sitemap_lastmod": (job.raw.get("sitemap") or {}).get("lastmod"),
        }
        job.raw["listing"] = listing_row
        job.raw["detail"] = {
            "row": row,
            "posted": True,
            "membership": parsed["membership"],
            "apply_status": parsed["apply_status"],
            "issues": parsed["issues"],
            "description_sha256": sha256(description.encode()).hexdigest(),
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": sections,
            "structured_sections": text_sections,
            "description_links": links,
            "role_attachments": [
                link for link in links if ATTACHMENT_SUFFIX.search(link.get("url") or "")
            ],
            "fields": {
                "job_id": str(row.get("id")),
                "requisition_id": row.get("displayJobId"),
                "ats_job_id": row.get("atsJobId"),
                "title": row.get("name"),
                "locations": row.get("locations"),
                "standardized_locations": row.get("standardizedLocations"),
                "department": row.get("department"),
                "workplace_type": row.get("workLocationOption"),
                "location_flexibility": row.get("locationFlexibility"),
                "posted_ts": row.get("postedTs"),
                "creation_ts": row.get("creationTs"),
                "public_url": row.get("publicUrl"),
                "position_url": row.get("positionUrl"),
                "employment_type": None,
                "salary": None,
            },
        }
        job.raw["date_fields"] = {
            "postedTs": row.get("postedTs"),
            "creationTs": row.get("creationTs"),
            "postedTs_mapping": "date_posted" if (row.get("postedTs") or 0) > 0 else None,
            "creationTs_mapping": None,
        }
        return job

    def _classify_hydrated(self):
        current, stale, unlisted, blocked, failed = [], [], [], [], []
        errors = list(self.result.errors)
        for job in self.result.jobs:
            detail = (job.raw.get("detail") or {}).get("row") or {}
            sitemap = job.raw.get("sitemap") or {}
            entry = {
                "id": job.job_id,
                "job_url": f"{PORTAL_ORIGIN}/careers/job/{job.job_id}",
                "sitemap_url": sitemap.get("url"),
            }
            if detail:
                posted = detail.get("postedTs")
                public_url = detail.get("publicUrl")
                canonical = f"{PORTAL_ORIGIN}/careers/job/{job.job_id}"
                is_posted = (
                    isinstance(posted, (int, float))
                    and posted > 0
                    and public_url == canonical
                )
                if is_posted and job.description_html:
                    current.append(job)
                elif not is_posted:
                    entry.update(
                        title=detail.get("name"),
                        posted_ts=posted,
                        public_url=public_url,
                        description_chars=len(job.description_html or ""),
                        reason=(
                            "detail postedTs missing or 0 and publicUrl null; sitemap "
                            "entry is not in the public posted listing membership "
                            "(closed drift, no description retained)"
                        ),
                    )
                    unlisted.append(entry)
                else:
                    entry.update(
                        title=detail.get("name"),
                        posted_ts=posted,
                        reason="posted detail returned no description",
                    )
                    failed.append(entry)
                continue
            error = next(
                (message for message in errors if job.url in message or str(job.job_id) in message),
                "",
            )
            entry["reason"] = error or "detail unavailable"
            if "HTTP 404" in error:
                stale.append(entry)
            elif "HTTP 405" in error or "SourceChallenge" in error or "WAF" in error:
                blocked.append(entry)
            else:
                failed.append(entry)
        removed_urls = {entry["job_url"] for entry in stale + unlisted + blocked}
        self.result.errors = [
            error
            for error in errors
            if not any(url in error for url in removed_urls)
        ]
        self.result.jobs = current
        return {
            "current": current,
            "stale": stale,
            "unlisted": unlisted,
            "blocked": blocked,
            "failed": failed,
            "unverified": [job for job in current if not job.description_verified],
        }

    async def run(self):
        source_started = time.perf_counter()
        self.challenge = None
        self.result.metadata.update(
            {
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "listing_url": LISTING_URL,
                "listing_authority": (
                    "Micron's own careers.micron.com Eightfold PCSX listing, "
                    "reconciled with the job sitemap published by its robots.txt."
                ),
                "listing_sample_request": {
                    "method": "GET",
                    "path": "/api/pcsx/search",
                    "query_shape": {
                        "domain": DOMAIN,
                        "query": "",
                        "location": "",
                        "start": "0,10,(pages-1)*10",
                        "sort_by": SORT_BY,
                    },
                    "note": "Published Latest sort; the UI Company-priority ranking reshuffled between page requests.",
                },
                "membership_request": {
                    "robots": ROBOTS_URL,
                    "sitemap_index": SITEMAP_INDEX_URL,
                    "job_sitemap": SITEMAP_URL,
                    "rule": (
                        "Sitemap URL must return a positive postedTs from the exact "
                        "PCSX position_details API; stale/404 or postedTs=0 entries are recorded separately."
                    ),
                },
                "detail_method": "micron.pcsx.position_details.jobDescription",
                "detail_request": {
                    "method": "GET",
                    "path": "/api/pcsx/position_details",
                    "query_shape": {"position_id": "sitemap id", "domain": DOMAIN, "hl": "en"},
                },
                "apply_route": f"{PORTAL_ORIGIN}/careers/apply?pid=<listing id>",
                "employer": EMPLOYER_NAME,
                "site_base_position_fq": BASE_POSITION_FQ,
                "site_excludes_china": "-location_country:CN",
            }
        )

        entry = await self._official_entry()
        self.result.metadata["official_entry"] = entry

        sample = await self._listing_sample()
        self.result.timings["listing_phase_seconds"] = sample["seconds"]
        if sample["issues"] or not sample["count"]:
            self.result.errors.extend(sample["issues"] or ["micron_listing_unavailable"])
            self.result.listing_complete = False
            return self.result

        sitemap = await self._sitemap_snapshot(advertised_count=sample["count"])
        self.result.timings["sitemap_phase_seconds"] = sitemap["seconds"]
        if sitemap["issues"]:
            self.result.errors.extend(sitemap["issues"])
            self.result.listing_complete = False
            return self.result

        self._add_sitemap_placeholders(sitemap["rows"])
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        classified = self._classify_hydrated()
        current = classified["current"]
        sitemap_ids = list(sitemap["ids"])
        membership = {
            "advertised_count": sample["count"],
            "advertised_pages": sample["pages"],
            "listing_sample_ids": sample["sample_ids"],
            "listing_samples": sample["samples"],
            "sitemap_url": sitemap["sitemap_url"],
            "sitemap_count": sitemap["count"],
            "sitemap_ids": sitemap_ids,
            "sitemap_ids_sha256": sha256("\n".join(sitemap_ids).encode()).hexdigest(),
            "sitemap_sha256": sitemap["sitemap_sha256"],
            "sitemap_lastmod_min": sitemap["lastmod_min"],
            "sitemap_lastmod_max": sitemap["lastmod_max"],
            "current_jobs": len(current),
            "stale_sitemap_entries": classified["stale"],
            "unlisted_sitemap_entries": classified["unlisted"],
            "blocked_sitemap_entries": classified["blocked"],
            "failed_sitemap_entries": classified["failed"],
            "unverified_jobs": [
                {
                    "id": job.job_id,
                    "url": job.url,
                    "warnings": job.warnings,
                }
                for job in classified["unverified"]
            ],
            "sample_ids_missing_from_sitemap": [
                identity for identity in sample["sample_ids"] if identity not in set(sitemap_ids)
            ],
        }
        self.result.metadata["membership_reconciliation"] = membership
        self.result.metadata["stale_sitemap_entries"] = classified["stale"]
        self.result.metadata["unlisted_sitemap_entries"] = classified["unlisted"]
        self.result.metadata["blocked_sitemap_entries"] = classified["blocked"]
        self.result.metadata["failed_sitemap_entries"] = classified["failed"]

        if self.challenge:
            self.result.errors.append(self.challenge)
            membership.update(
                {
                    "blocked_current_jobs": len(classified["blocked"]),
                    "final_recheck_performed": False,
                    "final_recheck_blocked": True,
                    "final_advertised_count": None,
                    "final_sitemap_count": None,
                    "final_sitemap_ids_sha256": None,
                    "new_sitemap_ids": [],
                    "removed_sitemap_ids": [],
                    "initial_final_id_order_matches": None,
                    "recheck_seconds": None,
                }
            )
            self.result.expected_total = sample["count"]
            self.result.listing_complete = False
            self.result.metadata["collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        final_started = time.perf_counter()
        final_listing = await self._listing_page(0)
        final_sitemap = await self._sitemap_snapshot(
            advertised_count=sample["count"]
        )
        final_ids = list(final_sitemap["ids"])
        new_ids = [identity for identity in final_ids if identity not in set(sitemap_ids)]
        removed_ids = [identity for identity in sitemap_ids if identity not in set(final_ids)]
        membership.update(
            {
                "final_advertised_count": final_listing["count"],
                "final_advertised_pages": final_listing["pages"],
                "final_sitemap_count": final_sitemap["count"],
                "final_sitemap_ids_sha256": sha256(
                    "\n".join(final_ids).encode()
                ).hexdigest(),
                "final_sitemap_sha256": final_sitemap["sitemap_sha256"],
                "new_sitemap_ids": new_ids,
                "removed_sitemap_ids": removed_ids,
                "initial_final_id_order_matches": sitemap_ids == final_ids,
                "recheck_seconds": max(0.0, time.perf_counter() - final_started),
            }
        )
        self.result.timings["final_recheck_phase_seconds"] = membership["recheck_seconds"]
        if final_listing["count"] != sample["count"]:
            self.result.errors.append("micron_listing_count_changed_during_collection")
        if new_ids or removed_ids:
            self.result.errors.append("micron_sitemap_changed_during_collection")
        if final_sitemap["issues"]:
            self.result.errors.extend(final_sitemap["issues"])
        self.result.expected_total = sample["count"]
        self.result.listing_complete = (
            len(current) == sample["count"]
            and final_listing["count"] == sample["count"]
            and not new_ids
            and not removed_ids
            and not classified["failed"]
            and not classified["blocked"]
            and not classified["unverified"]
            and not self.result.errors
        )
        self.result.metadata["collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
