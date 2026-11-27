"""InMobi's official careers page, its company listing API, and exact Greenhouse embeds.

Observed journey (see companies/inmobi/journey-research.md):

- The official entry ``https://www.inmobi.com/company/careers`` is InMobi's own
  Next.js careers page. Its ``__NEXT_DATA__`` embeds ``allCareersData[0].jobArr``
  (79 roles at the inspected snapshot) with ``jobId``, ``reqId``, ``jobTitle``,
  ``jobLocation``, ``jobCountry`` and ``jobDepartment``.
- The page's own server-side loader requests ``/api/v1/careersapi``; that exact
  company endpoint returns the same ``jobArr``. The listing has no server-side
  pagination or filtering: page/limit/country/department query strings are
  ignored and the unfiltered 79-row array is returned every time. The UI groups
  those rows by department and offers a client-side country dropdown/search.
- Each company detail route is
  ``/company/openings/<department-slug>/jobid/<jobId>``. The server-rendered
  page contains the title and ``Job ID: <reqId>`` and embeds exactly one
  ``iframe#job-iframe`` whose src is
  ``https://boards.greenhouse.io/inmobi/jobs/<jobId>`` (which redirects to
  ``https://job-boards.greenhouse.io/inmobi/jobs/<jobId>``). The company page
  does not render the description itself; the full description lives in that
  company-published Greenhouse embed.
- The exact public Greenhouse job endpoint for an exposed ID,
  ``https://boards-api.greenhouse.io/v1/boards/inmobi/jobs/<jobId>``, returns
  the same board row the iframe renders, including the full ``content`` HTML,
  ``first_published``, ``updated_at``, ``requisition_id``, ``location``,
  ``metadata`` and ``departments``. Only IDs exposed by InMobi's own listing
  are requested; the board is never enumerated and no other tenant is touched.
- The InMobi listing is the membership authority. The Greenhouse board's
  published job set happened to equal the company listing at the research
  snapshot (79/79), which is recorded as a cross-check, not as the source.

Dates are source-only: ``first_published``/``updated_at`` are retained and
``application_deadline`` stays null when the source publishes null. Salary is
captured only when the employer's own description publishes an explicit base
salary range; nothing is inferred.
"""
from __future__ import annotations

import html as html_module
import json
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.inmobi.com/company/careers"
LISTING_API = "https://www.inmobi.com/api/v1/careersapi"
COMPANY_DETAIL_BASE = "https://www.inmobi.com/company/openings"
GREENHOUSE_EMBED_BASE = "https://boards.greenhouse.io/inmobi/jobs"
GREENHOUSE_API_BASE = "https://boards-api.greenhouse.io/v1/boards/inmobi/jobs"
GREENHOUSE_BOARD = "inmobi"
GREENHOUSE_JOB_PAGE_HOST = "job-boards.greenhouse.io"
ENTRY_HOST = "www.inmobi.com"
ENTRY_PATH = "/company/careers"
LISTING_PATH = "/api/v1/careersapi"
DETAIL_PATH = re.compile(r"^/company/openings/([a-z0-9-]+)/jobid/(\d+)$")
GREENHOUSE_API_PATH = re.compile(r"^/v1/boards/inmobi/jobs/(\d+)$")
GREENHOUSE_PAGE_PATH = re.compile(r"^/inmobi/jobs/(\d+)$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SALARY_RANGE = re.compile(
    r"\bbase\b[^$]{0,140}?\$([\d,]+)\s*(?:USD)?\s*(?:to|–|—|-)\s*\$([\d,]+)\s*(?:USD)?",
    re.I,
)
RESPONSIBILITY_HEADINGS = (
    "what will you be doing", "what you'll do", "what you’ll do", "what you will do",
    "responsibilit", "the impact you'll make", "the impact you’ll make",
    "what you'll build", "what you’ll build", "day-to-day", "your day-to-day",
)
QUALIFICATION_HEADINGS = (
    "role requirement", "requirements", "what is expected", "what we're looking for",
    "what we’re looking for", "qualif", "preferred", "the experience we need",
    "who you are", "skills", "experience",
)
DESCRIPTION_MIN_CHARS = 100


def normalized_title(value: str | None) -> str:
    """Normalize only Unicode composition and whitespace; preserve wording."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def rendered_text(value: str) -> str:
    """Approximate visible inline text and collapse browser whitespace for audits."""
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+([,;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    result = 14695981039346656037
    for char in value:
        result = ((result ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{result:016x}"


def department_slug(department: str) -> str:
    """Reproduce the careers page's exact slug rule for a listing department."""
    if not isinstance(department, str) or not department.strip():
        raise ValueError("InMobi listing row has no department for its detail route")
    value = re.sub(r"\s+", "-", department).lower()
    return value.replace("&", "and").replace(",", "")


def company_detail_url(row: dict) -> str:
    identity = row.get("jobId")
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError("InMobi listing row has an invalid exact job ID")
    return f"{COMPANY_DETAIL_BASE}/{department_slug(row.get('jobDepartment'))}/jobid/{identity}"


def greenhouse_embed_url(identity) -> str:
    identity = str(identity)
    if not identity.isdigit():
        raise ValueError(f"InMobi job ID is not numeric: {identity!r}")
    return f"{GREENHOUSE_EMBED_BASE}/{identity}"


def greenhouse_api_url(identity) -> str:
    identity = str(identity)
    if not identity.isdigit():
        raise ValueError(f"InMobi job ID is not numeric: {identity!r}")
    return f"{GREENHOUSE_API_BASE}/{identity}"


def _validate_listing_url(url: str, label: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ENTRY_HOST or parsed.path.rstrip("/") != LISTING_PATH:
        raise ValueError(f"InMobi {label} listing request left the inspected company endpoint: {url}")


def _validate_entry_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ENTRY_HOST or parsed.path.rstrip("/") != ENTRY_PATH:
        raise ValueError(f"InMobi official entry left the inspected company page: {url}")


def _validate_greenhouse_url(url: str, identity) -> None:
    parsed = urlsplit(url)
    match = GREENHOUSE_API_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "boards-api.greenhouse.io"
            or match is None or match.group(1) != str(identity)):
        raise ValueError(f"InMobi exact Greenhouse detail request left the exposed job: {url}")


def _validate_listing_rows(rows, label: str) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError(f"InMobi {label} listing has no job rows array")
    identities = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"InMobi {label} listing row is not an object")
        identity = row.get("jobId")
        if not isinstance(identity, int) or identity <= 0:
            raise ValueError(f"InMobi {label} listing row has a missing or invalid exact job ID")
        if not isinstance(row.get("jobTitle"), str) or not row["jobTitle"].strip():
            raise ValueError(f"InMobi {label} listing row {identity} has no title")
        requisition = row.get("reqId")
        if not isinstance(requisition, (str, int)) or not str(requisition).strip():
            raise ValueError(f"InMobi {label} listing row {identity} has no requisition ID")
        if not isinstance(row.get("jobLocation"), str) or not row["jobLocation"].strip():
            raise ValueError(f"InMobi {label} listing row {identity} has no location")
        countries = row.get("jobCountry")
        if not isinstance(countries, list) or not countries or not all(
                isinstance(value, str) and value.strip() for value in countries):
            raise ValueError(f"InMobi {label} listing row {identity} has no country list")
        if not isinstance(row.get("jobDepartment"), str) or not row["jobDepartment"].strip():
            raise ValueError(f"InMobi {label} listing row {identity} has no department")
        identities.append(str(identity))
    if len(identities) != len(set(identities)):
        raise ValueError(f"InMobi {label} listing repeats a company-published job ID")
    return rows


def parse_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read InMobi's official careers page and its embedded company listing."""
    _validate_entry_url(response_url)
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at InMobi" not in title:
        raise ValueError("InMobi official careers page identity changed")
    if soup.select_one("#jobs h2") is None:
        raise ValueError("InMobi official careers page no longer exposes the Current Openings section")
    node = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
    if node is None or not node.string:
        raise ValueError("InMobi official careers page no longer embeds its Next.js data")
    try:
        data = json.loads(node.string)
    except ValueError as exc:
        raise ValueError("InMobi official careers page embedded data is not valid JSON") from exc
    page_props = data.get("props", {}).get("pageProps", {})
    careers = page_props.get("allCareersData")
    if not isinstance(careers, list) or len(careers) != 1 or not isinstance(careers[0], dict):
        raise ValueError("InMobi official careers page embedded data has no single careers object")
    rows = _validate_listing_rows(careers[0].get("jobArr"), "embedded")
    countries = careers[0].get("countryArr")
    if not isinstance(countries, list) or not all(isinstance(value, str) and value.strip() for value in countries):
        raise ValueError("InMobi official careers page embedded country list is missing")
    return {
        "url": response_url,
        "title": title,
        "rows": rows,
        "countries": countries,
        "heading": normalized_title(soup.select_one("#jobs h2").get_text(" ", strip=True)),
        "meta_title": careers[0].get("meta_title"),
        "meta_description": careers[0].get("meta_description"),
    }


def parse_listing(payload, response_url: str = LISTING_API) -> dict:
    """Read the exact unfiltered company listing request made by the careers page."""
    _validate_listing_url(response_url, "published")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("InMobi careers API response is not the single company careers object")
    value = payload[0]
    rows = _validate_listing_rows(value.get("jobArr"), "API")
    countries = value.get("countryArr")
    if not isinstance(countries, list) or not all(isinstance(item, str) and item.strip() for item in countries):
        raise ValueError("InMobi careers API response has no country list")
    present = {country for row in rows for country in row["jobCountry"]}
    if present != set(countries):
        raise ValueError("InMobi careers API country list no longer matches the listing countries")
    return {
        "url": response_url,
        "rows": rows,
        "countries": countries,
        "count": len(rows),
        "department_counts": {
            department: sum(1 for row in rows if row["jobDepartment"] == department)
            for department in sorted({row["jobDepartment"] for row in rows})
        },
    }


def parse_company_detail(html: str, response_url: str, row: dict) -> dict:
    """Validate one exact company detail page and the Greenhouse embed it publishes."""
    expected_url = company_detail_url(row)
    if response_url.rstrip("/") != expected_url.rstrip("/"):
        raise ValueError("InMobi company detail response is not the exact listed job route")
    soup = BeautifulSoup(html or "", "lxml")
    expected_title = f"InMobi Job Opening - {row['jobTitle'].strip()}"
    actual_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if actual_title != normalized_title(expected_title):
        raise ValueError(f"InMobi company detail {row['jobId']} title changed")
    heading = soup.select_one("h1")
    if heading is None or normalized_title(heading.get_text(" ", strip=True)) != normalized_title(row["jobTitle"]):
        raise ValueError(f"InMobi company detail {row['jobId']} heading does not match the listing")
    identity_text = soup.select_one("#jod-id")
    expected_identity = f"Job ID: {row['reqId']}"
    if identity_text is None or normalized_title(identity_text.get_text(" ", strip=True)) != expected_identity:
        raise ValueError(f"InMobi company detail {row['jobId']} requisition label changed")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or canonical.get("href", "").rstrip("/") != expected_url:
        raise ValueError(f"InMobi company detail {row['jobId']} canonical URL changed")
    iframes = soup.select("iframe#job-iframe")
    if len(iframes) != 1:
        raise ValueError(f"InMobi company detail {row['jobId']} no longer publishes one job iframe")
    embed_url = iframes[0].get("src", "")
    if embed_url != greenhouse_embed_url(row["jobId"]):
        raise ValueError(f"InMobi company detail {row['jobId']} Greenhouse embed URL changed")
    return {
        "url": expected_url,
        "title": actual_title,
        "requisition_label": expected_identity,
        "iframe_src": embed_url,
        "canonical_url": canonical.get("href"),
    }


def decode_content(value: str) -> str:
    """Greenhouse returns the description HTML entity-escaped; decode exactly once."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("InMobi Greenhouse job detail has no description content")
    stripped = value.lstrip()
    if stripped.startswith("&lt;"):
        return html_module.unescape(value)
    return value


def _is_heading_block(node: Tag) -> str | None:
    if node.name in HEADING_TAGS:
        value = normalized_title(node.get_text(" ", strip=True))
        return value or None
    if node.name not in ("p", "div"):
        return None
    strongs = node.find_all(["strong", "b"])
    if len(strongs) != 1:
        return None
    value = normalized_title(strongs[0].get_text(" ", strip=True))
    if not value or len(value) > 140:
        return None
    remainder = normalized_title(node.get_text(" ", strip=True)).replace(value, "", 1).strip()
    if remainder:
        return None
    if value.endswith((":", "?")) or len(value.split()) <= 12:
        return value
    return None


def _blocks(description_html: str) -> list[Tag]:
    soup = BeautifulSoup(description_html or "", "lxml")
    root = soup.body or soup
    blocks: list[Tag] = []
    for node in root.children:
        if not isinstance(node, Tag):
            continue
        if node.name == "div" and set(node.get("class") or []) & {"content-intro", "content-conclusion"}:
            blocks.extend(child for child in node.children if isinstance(child, Tag))
        else:
            blocks.append(node)
    return blocks


def description_sections(description_html: str) -> list[dict]:
    """Split the publisher HTML at its bold/heading labels without inventing text."""
    sections: list[dict] = []
    current = {"heading": None, "nodes": []}
    for node in _blocks(description_html):
        heading = _is_heading_block(node)
        if heading:
            if current["nodes"]:
                sections.append(current)
            current = {"heading": heading, "nodes": [node]}
        else:
            current["nodes"].append(node)
    if current["nodes"]:
        sections.append(current)
    result = []
    for section in sections:
        html_value = "".join(str(node) for node in section["nodes"])
        text_value = text_html(html_value).strip()
        if not text_value:
            continue
        result.append({"heading": section["heading"], "html": html_value, "text": text_value})
    return result


def _matching_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    matched = []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if heading and any(token in heading for token in tokens):
            matched.append(section)
    return matched or None


def salary_from_text(description_text: str) -> dict | None:
    """Capture an explicit employer-published base salary range, or nothing."""
    text = re.sub(r"\s+", " ", (description_text or "").replace("\xa0", " ")).strip()
    match = SALARY_RANGE.search(text)
    if match is None:
        return None
    minimum = int(match.group(1).replace(",", ""))
    maximum = int(match.group(2).replace(",", ""))
    if maximum < minimum:
        return None
    start = text.rfind(". ", 0, match.start())
    start = 0 if start < 0 else start + 2
    end = text.find(". ", match.end())
    end = len(text) if end < 0 else end + 1
    sentence = text[start:end].strip()
    currency = "USD" if "USD" in match.group(0).upper() or "$" in match.group(0) else None
    return {
        "currency": currency,
        "min": minimum,
        "max": maximum,
        "basis": "annual base salary (fixed)",
        "text": sentence,
        "source": "greenhouse.content",
    }


def _attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT_SUFFIX.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def parse_greenhouse_detail(payload, response_url: str, row: dict) -> dict:
    """Validate the exact public Greenhouse job record for one exposed InMobi ID."""
    identity = str(row["jobId"])
    _validate_greenhouse_url(response_url, identity)
    if not isinstance(payload, dict):
        raise ValueError(f"InMobi Greenhouse detail {identity} is not an object")
    if str(payload.get("id")) != identity:
        raise ValueError(f"InMobi Greenhouse detail ID changed for {identity}")
    title = normalized_title(payload.get("title"))
    if title != normalized_title(row["jobTitle"]):
        raise ValueError(f"InMobi Greenhouse detail {identity} title differs from the listing")
    if str(payload.get("requisition_id")) != str(row["reqId"]):
        raise ValueError(f"InMobi Greenhouse detail {identity} requisition differs from the listing")
    company_name = normalized_title(payload.get("company_name"))
    if not company_name or not company_name.lower().startswith("inmobi"):
        raise ValueError(f"InMobi Greenhouse detail {identity} employer changed")
    location = payload.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    if normalized_title(location_name) != normalized_title(row["jobLocation"]):
        raise ValueError(f"InMobi Greenhouse detail {identity} location differs from the listing")
    absolute_url = payload.get("absolute_url") or ""
    parsed = urlsplit(absolute_url)
    page_match = GREENHOUSE_PAGE_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != GREENHOUSE_JOB_PAGE_HOST
            or page_match is None or page_match.group(1) != identity):
        raise ValueError(f"InMobi Greenhouse detail {identity} job/apply URL changed")

    metadata_rows = payload.get("metadata") or []
    metadata = {}
    for item in metadata_rows:
        if isinstance(item, dict) and item.get("name"):
            metadata[item["name"]] = item.get("value")
    if metadata.get("Employment Type") != row["jobDepartment"]:
        raise ValueError(f"InMobi Greenhouse detail {identity} employment type differs from the listing")
    if metadata.get("Base Country") != row["jobCountry"]:
        raise ValueError(f"InMobi Greenhouse detail {identity} base country differs from the listing")

    description_html = decode_content(payload.get("content"))
    description_text = text_html(description_html).strip()
    if len(description_text) < DESCRIPTION_MIN_CHARS:
        raise ValueError(f"InMobi Greenhouse detail {identity} description is empty or truncated")
    sections = description_sections(description_html)
    salary = salary_from_text(description_text)
    departments = [
        item.get("name") for item in (payload.get("departments") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    offices = payload.get("offices") if isinstance(payload.get("offices"), list) else []
    deadline = payload.get("application_deadline")
    first_published = payload.get("first_published")
    updated_at = payload.get("updated_at")
    return {
        "payload": payload,
        "title": title,
        "requisition_id": str(payload["requisition_id"]),
        "company_name": company_name,
        "description_html": description_html,
        "description_text": description_text,
        "description_sha256": sha256(description_text.encode()).hexdigest(),
        "sections": sections,
        "responsibilities": _matching_sections(sections, RESPONSIBILITY_HEADINGS),
        "qualifications": _matching_sections(sections, QUALIFICATION_HEADINGS),
        "salary": salary,
        "metadata": metadata,
        "departments": departments,
        "offices": offices,
        "date_posted": first_published if isinstance(first_published, str) and first_published.strip() else None,
        "date_posted_raw": first_published,
        "date_updated": updated_at if isinstance(updated_at, str) and updated_at.strip() else None,
        "valid_through": deadline if isinstance(deadline, str) and deadline.strip() else None,
        "language": payload.get("language") if isinstance(payload.get("language"), str) else None,
        "apply_url": absolute_url,
        "attachment_links": _attachment_links(description_html),
    }


def ui_order(rows: list[dict]) -> list[str]:
    """Reproduce the careers page grouping: departments sorted, served order inside."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row["jobDepartment"], []).append(row)
    ordered = []
    for department in sorted(grouped):
        ordered.extend(grouped[department])
    return [str(row["jobId"]) for row in ordered]


class InMobi(Adapter):
    """Follow InMobi's official careers page and its exact Greenhouse embeds."""

    VERIFICATION_REVISION = "inmobi.official-entry.company-api.exact-greenhouse-embeds.1"
    REPRESENTATIVE_COUNT = 2

    async def _entry_snapshot(self):
        response = await self.http.get(OFFICIAL_ENTRY)
        parsed = parse_entry(response.text, str(response.url))
        parsed.update({
            "status": int(response.status_code),
            "final_url": str(response.url),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _listing_snapshot(self, label: str):
        response = await self.http.get(LISTING_API)
        if str(response.url).rstrip("/") != LISTING_API:
            raise ValueError(f"InMobi {label} listing request redirected away from the inspected endpoint")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"InMobi {label} listing response is not JSON") from exc
        parsed = parse_listing(payload, str(response.url))
        parsed.update({
            "status": int(response.status_code),
            "final_url": str(response.url),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _company_detail_check(self, row: dict):
        url = company_detail_url(row)
        response = await self.http.get(url)
        parsed = parse_company_detail(response.text, str(response.url), row)
        parsed.update({
            "status": int(response.status_code),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    def _make_job(self, row: dict, entry_row: dict | None = None):
        identity = str(row["jobId"])
        job = self.job(
            {
                "listing": row,
                "entry_listing": entry_row if entry_row is not None else row,
                "company_detail_url": company_detail_url(row),
                "greenhouse_embed_url": greenhouse_embed_url(identity),
                "greenhouse_api_url": greenhouse_api_url(identity),
                "greenhouse_board": GREENHOUSE_BOARD,
            },
            title=row["jobTitle"].strip(),
            url=company_detail_url(row),
            job_id=identity,
            requisition_id=str(row["reqId"]),
            apply_url=greenhouse_embed_url(identity),
            description="",
            method=None,
            description_url=greenhouse_api_url(identity),
            locations=[{"name": row["jobLocation"], "countries": list(row["jobCountry"])}],
            employment_type=row["jobDepartment"],
            employer="InMobi",
            language=None,
        )
        job.record_identity = f"inmobi:{identity}"
        return job

    async def _detail(self, job):
        row = job.raw["listing"]
        identity = str(row["jobId"])
        url = greenhouse_api_url(identity)
        response = await self.http.get(url)
        parsed = parse_greenhouse_detail(response.json(), str(response.url), row)
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "inmobi.official_entry.greenhouse_exact_job_api"
        job.description_source_url = url
        job.department = parsed["metadata"].get("Department Name (Career Site)")
        job.employment_type = parsed["metadata"].get("Employment Type") or row["jobDepartment"]
        job.team = "; ".join(parsed["departments"]) if parsed["departments"] else None
        job.employer = parsed["company_name"]
        job.language = parsed["language"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.date_updated = parsed["date_updated"]
        job.valid_through = parsed["valid_through"]
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.raw["greenhouse"] = parsed["payload"]
        job.raw["greenhouse_parsed"] = {
            key: parsed[key] for key in (
                "metadata", "departments", "offices", "sections", "salary",
                "date_posted", "date_updated", "valid_through", "attachment_links",
            )
        }
        job.raw["detail_response"] = {
            "url": str(response.url),
            "status": int(response.status_code),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        return job

    async def run(self):
        started = self.result._started_clock
        _validate_entry_url(self.company.careers_url)
        if not self.page_guard():
            return self.result

        entry = await self._entry_snapshot()
        initial = await self._listing_snapshot("initial")
        rows = initial["rows"]
        entry_rows = entry["rows"]
        entry_matches_api = entry_rows == rows
        if not entry_matches_api:
            self.result.errors.append(
                "inmobi_official_entry_embedded_listing_differs_from_the_page_api"
            )
        if not rows:
            self.result.expected_total = 0
            self.result.errors.append("inmobi_company_listing_returned_zero_jobs")
            self.result.metadata.update(self._metadata(entry, initial, None, {}, {}, False, False))
            return self.result

        self.result.expected_total = len(rows)
        self.result.pages = 1
        entry_by_id = {str(row["jobId"]): row for row in entry_rows}
        jobs = [self._make_job(row, entry_by_id.get(str(row["jobId"]))) for row in rows]
        for job in jobs:
            self.add(job)

        representative_started = time.perf_counter()
        representative = {}
        representative_errors = []
        indexes = sorted({0, len(rows) - 1})[: self.REPRESENTATIVE_COUNT]
        for index in indexes:
            row = rows[index]
            identity = str(row["jobId"])
            try:
                representative[identity] = await self._company_detail_check(row)
            except Exception as exc:
                representative_errors.append(f"{identity}: {type(exc).__name__}: {exc}")
        if representative_errors:
            self.result.errors.extend(
                f"InMobi representative company detail {item}" for item in representative_errors
            )
        representative_seconds = max(0.0, time.perf_counter() - representative_started)

        detail_started = time.perf_counter()
        hydrated = await self.hydrate(jobs, self._detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_snapshot_verified = True
        try:
            final = await self._listing_snapshot("final")
        except Exception as exc:
            final_snapshot_verified = False
            final = {"rows": rows, "count": len(rows), "countries": initial["countries"],
                     "department_counts": initial["department_counts"]}
            self.result.errors.append(
                f"InMobi final company listing snapshot failed; initial usable rows retained: {type(exc).__name__}: {exc}"
            )
        final_rows = final["rows"]
        initial_ids = {str(row["jobId"]) for row in rows}
        final_ids = {str(row["jobId"]) for row in final_rows}
        membership_changed = final_snapshot_verified and initial_ids != final_ids
        if membership_changed:
            self.result.errors.append(
                "inmobi_company_listing_membership_changed_between_initial_and_final_snapshots"
            )
        initial_by_id = {str(row["jobId"]): row for row in rows}
        final_by_id = {str(row["jobId"]): row for row in final_rows}
        changed_rows = sorted(
            identity for identity in initial_ids & final_ids
            if initial_by_id[identity] != final_by_id[identity]
        )
        if changed_rows:
            self.result.errors.append("inmobi_company_listing_rows_changed_between_snapshots")

        failed = [
            {"id": job.job_id, "url": job.url, "reason": "; ".join(job.warnings) or "description_not_verified"}
            for job in hydrated if not job.description_verified
        ]
        self.result.listing_complete = bool(
            not self.result.errors
            and final_snapshot_verified
            and not membership_changed
            and not changed_rows
            and len(hydrated) == len(rows)
            and all(job.description_verified for job in hydrated)
        )
        self.result.metadata.update(self._metadata(
            entry, initial, final, representative, failed,
            membership_changed, final_snapshot_verified,
            initial_ids=initial_ids, final_ids=final_ids,
            changed_rows=changed_rows, entry_matches_api=entry_matches_api,
        ))
        self.result.metadata["initial_ui_order"] = ui_order(rows)
        self.result.metadata["final_ui_order"] = ui_order(final_rows)
        self.result.timings["source_collection_seconds"] = round(representative_seconds + detail_seconds, 6)
        self.result.timings["representative_company_detail_seconds"] = round(representative_seconds, 6)
        self.result.timings["detail_hydration_seconds"] = round(detail_seconds, 6)
        self.result.timings["listing_snapshot_seconds"] = round(
            max(0.0, time.perf_counter() - started) - representative_seconds - detail_seconds, 6
        )
        return self.result

    def _metadata(self, entry, initial, final, representative, failed,
                  membership_changed, final_snapshot_verified, *, initial_ids=None,
                  final_ids=None, changed_rows=None, entry_matches_api=None):
        rows = initial["rows"]
        return {
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_status": entry.get("status"),
            "official_entry_final_url": entry.get("final_url"),
            "official_entry_response_sha256": entry.get("response_sha256"),
            "listing_api_url": LISTING_API,
            "listing_api_status": initial.get("status"),
            "listing_api_final_url": initial.get("final_url"),
            "listing_api_response_sha256": initial.get("response_sha256"),
            "listing_rows": len(rows),
            "listing_countries": initial["countries"],
            "listing_department_counts": initial["department_counts"],
            "entry_embedded_rows": len(entry["rows"]),
            "entry_embedded_listing_matches_page_api": entry_matches_api,
            "membership_authority": (
                "InMobi's own official careers page at www.inmobi.com/company/careers and the exact "
                "unfiltered listing request it makes to www.inmobi.com/api/v1/careersapi. The page renders "
                "every returned row; there is no server-side pagination or filtering. Each role's company "
                "detail route is /company/openings/<department>/jobid/<jobId>."
            ),
            "pagination": (
                "None. The careers API ignores page/limit/country/department query parameters and always "
                "returns the full jobArr; the UI groups all rows by department and filters client-side."
            ),
            "ui_order": ui_order(rows),
            "ui_order_note": (
                "Computed from the careers page grouping rule (departments sorted, titles sorted inside); "
                "the independent browser reference records the actual DOM order for comparison."
            ),
            "detail_source": (
                "Exact Greenhouse job endpoint for each ID exposed by InMobi's listing "
                "(boards-api.greenhouse.io/v1/boards/inmobi/jobs/<jobId>); the company detail page embeds "
                "boards.greenhouse.io/inmobi/jobs/<jobId>, which redirects to job-boards.greenhouse.io. "
                "The board is never enumerated and no other board or slug is requested."
            ),
            "greenhouse_board": GREENHOUSE_BOARD,
            "representative_company_details": representative,
            "failed_jobs": failed,
            "initial_rows": len(rows),
            "initial_listing_ids": [str(row["jobId"]) for row in rows],
            "final_rows": len((final or {}).get("rows") or []),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_ids": sorted(initial_ids or []),
            "final_ids": sorted(final_ids or []),
            "membership_changed": membership_changed,
            "changed_rows": changed_rows or [],
            "salary_policy": (
                "No structured pay field is published on the current rows; salary is captured only when the "
                "employer's own description states an explicit base salary range, with the exact sentence retained."
            ),
            "description_min_chars": DESCRIPTION_MIN_CHARS,
            "collection_wall_seconds": round(time.perf_counter() - self.result._started_clock, 6),
        }
