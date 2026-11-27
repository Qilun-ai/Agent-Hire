"""Netflix's own Eightfold careers listing and its exact job detail pages.

The official entry ``https://explore.jobs.netflix.net/careers`` is Netflix's
published careers listing (the old ``jobs.netflix.com`` brand page links its
"VIEW ROLES" calls to this URL).  The page is an Eightfold PCS shell: the first
ten cards are server-rendered from ``code#smartApplyData`` and the "Show More
Positions" control requests the same public listing endpoint ten rows at a
time.  Each card opens ``/careers/job/<id>`` and the exact detail API returns
the employer's full ``job_description`` field.  The detail page's JSON-LD
JobPosting supplies the published ``datePosted`` and ``employmentType``.

Only the company-scoped ``netflix.com`` microsite is enumerated.  Netflix House
is a separate Eightfold microsite (``apply.netflixhouse.com``) linked from the
navigation; its roles are not in this listing and are not mixed into this
company's membership.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://explore.jobs.netflix.net/careers"
LISTING_URL = OFFICIAL_ENTRY
ORIGIN = "https://explore.jobs.netflix.net"
DOMAIN = "netflix.com"
LISTING_API = ORIGIN + "/api/apply/v2/jobs"
DETAIL_API = ORIGIN + "/api/apply/v2/jobs/{job_id}"
JOB_URL = ORIGIN + "/careers/job/{job_id}"
APPLY_URL = ORIGIN + "/careers/apply?pid={job_id}"
PAGE_SIZE = 10
SORT_BY = "relevance"
EMPLOYER_NAME = "Netflix"
JOB_PATH = re.compile(r"^/careers/job/(\d+)$")
JOB_DETAIL_PATH = re.compile(r"^/careers/job/(\d+)(?:-[^/]*)?$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?$")
POSTING_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
EXPRESSION_OF_INTEREST_RE = re.compile(r"expression of interest", re.I)
INTERNSHIP_RE = re.compile(r"\bintern(?:ship)?\b", re.I)
EARLY_CAREER_RE = re.compile(r"\b(?:new grad|graduate|early career)\b", re.I)


class SourceChallenge(RuntimeError):
    """The source returned a bot-protection or rate-limit response."""


# Headings that start a new employer-authored section.  These are deliberately
# specific: bold sub-headings inside a Responsibilities block (for example
# "Budgets and cost") must stay inside that block rather than becoming a new
# top-level section.
MAJOR_HEADINGS = (
    "responsibilit",
    "requirement",
    "qualification",
    "what you'll do",
    "what you will do",
    "what will you do",
    "in this role",
    "this pm will",
    "as an engineering manager",
    "key responsibilities",
    "job responsibilities",
    "technical responsibilities",
    "role & responsibilities",
    "job summary",
    "position summary",
    "role overview",
    "summary",
    "the role",
    "about the role",
    "the opportunity",
    "about you",
    "who you are",
    "who are you",
    "what we're looking for",
    "what we are looking for",
    "who we are looking for",
    "we're looking for",
    "what sets you apart",
    "nice to have",
    "minimum qualifications",
    "preferred qualifications",
    "additional qualifications",
    "required qualifications",
    "skills & experience",
    "experience & skills",
    "education & experience",
    "desired skills",
    "the must-haves",
    "bonus skills",
    "the ideal candidate",
    "to succeed in this role",
    "the successful candidate",
    "qualities we're looking for",
    "what (ideally) do you know",
    "you will be successful in this role",
    "what you bring",
    "what you'll bring",
    "additional skills",
    "skills/knowledge",
    "experience with the following is a bonus",
    "our team",
    "about the team",
    "the team",
    "why netflix",
    "what we offer",
    "benefits",
    "compensation",
    "location",
    "job description",
    "inclusion is a netflix value",
    "we are an equal-opportunity employer",
    "netflix provides comprehensive benefits",
    "netflix is a unique culture",
    "job is open for no less than",
)

RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|what you(?:'|’)ll do|what you will do|what will you do|"
    r"in this role|this pm will|as an engineering manager|role & responsibilities",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualification|requirement|what you(?:'|’)ll need|what you will need|"
    r"what we(?:'|’)re looking for|what we are looking for|who we are looking for|"
    r"who you are|who are you|about you|nice to have|skills?|experience|education|"
    r"must-haves|bonus|ideal candidate|to succeed in this role|successful candidate|"
    r"qualities|what \(ideally\)|you will be successful|what you bring|"
    r"what you(?:'|’)ll bring|additional skills",
    re.I,
)

SALARY_RANGE = re.compile(
    r"\$?\s*(?P<min>\d[\d,]*(?:\.\d+)?)\s*(?P<mink>[kK])?"
    r"\s*(?:/\s*(?:hour|hr)|per\s+hour|an\s+hour)?\s*"
    r"(?:-|–|—|to)\s*"
    r"\$?\s*(?P<max>\d[\d,]*(?:\.\d+)?)\s*(?P<maxk>[kK])?"
    r"\s*(?:/\s*(?:hour|hr)|per\s+hour|an\s+hour)?"
    r"\s*(?P<currency>USD|CAD|EUR|GBP)?",
    re.I,
)
SALARY_SINGLE = re.compile(
    r"\$\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kK])?"
    r"(?:\s*(?:/\s*(?:hour|hr)|per\s+hour|an\s+hour))?",
    re.I,
)
CBA_RATE = re.compile(
    r"no less than\s*\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kK])?"
    r"\s*(?:an hour|/\s*hour|per hour)",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace/punctuation normalisation used for body hashes and audits."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True).replace("\xa0", " ")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_plain_text(value: str | None) -> str:
    value = (value or "").replace("\xa0", " ")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def comparison_key(value: str | None) -> str:
    """Compare prose while ignoring Markdown bullets/headings and whitespace.

    The API body is rendered HTML and the page's JSON-LD is a Markdown-flavoured
    text rendering of the same employer body.  This key is used only for the
    completeness cross-check, not as the stored description or its hash.
    """
    value = (value or "").replace("\xa0", " ")
    value = re.sub(r"\\(.)", r"\1", value)
    value = re.sub(r"[*#>`]", "", value)
    return re.sub(r"\s+", "", value)


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


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return sha256(value).hexdigest()


def _job_path(identity: str) -> str:
    return f"/careers/job/{identity}"


def _validate_listing_row(row: dict, where: str = "listing") -> dict:
    if not isinstance(row, dict):
        raise ValueError(f"Netflix {where} row is not an object")
    identity = row.get("id")
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"Netflix {where} row has no numeric id")
    name = normalized_title(row.get("name"))
    if not name:
        raise ValueError(f"Netflix {where} row {identity} has no title")
    if normalized_title(row.get("posting_name")) != name:
        raise ValueError(f"Netflix {where} row {identity} posting_name differs from name")
    if row.get("isPrivate") is not False:
        raise ValueError(f"Netflix {where} row {identity} is not a public posting")
    if row.get("type") != "ATS":
        raise ValueError(f"Netflix {where} row {identity} type changed")
    locations = row.get("locations")
    if not isinstance(locations, list) or not locations or not all(isinstance(x, str) and x.strip() for x in locations):
        raise ValueError(f"Netflix {where} row {identity} has no locations")
    if row.get("location") not in locations:
        raise ValueError(f"Netflix {where} row {identity} primary location is not in locations")
    ats_id = str(row.get("ats_job_id") or "").strip()
    if not ats_id:
        raise ValueError(f"Netflix {where} row {identity} has no requisition id")
    if str(row.get("display_job_id") or "") != ats_id:
        raise ValueError(f"Netflix {where} row {identity} display_job_id differs from ats_job_id")
    if not str(row.get("id_locale") or "").startswith(ats_id + "-"):
        raise ValueError(f"Netflix {where} row {identity} id_locale is not requisition scoped")
    for key in ("t_create", "t_update"):
        if not isinstance(row.get(key), int) or row[key] <= 0:
            raise ValueError(f"Netflix {where} row {identity} has no valid {key}")
    parsed = urlsplit(str(row.get("canonicalPositionUrl") or ""))
    if parsed.scheme != "https" or parsed.netloc != "explore.jobs.netflix.net" or parsed.path != _job_path(str(identity)):
        raise ValueError(f"Netflix {where} row {identity} canonical URL changed")
    if row.get("job_description") not in ("", None):
        raise ValueError(f"Netflix {where} row {identity} unexpectedly embeds a listing body")
    return row


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "explore.jobs.netflix.net"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Netflix official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Careers at Netflix":
        raise ValueError("Netflix official careers page title changed")
    canonical = soup.select_one('link[rel="canonical"]')
    if not canonical or canonical.get("href") != OFFICIAL_ENTRY:
        raise ValueError("Netflix official careers page canonical URL changed")
    node = soup.find("code", id="smartApplyData")
    if node is None:
        raise ValueError("Netflix official careers page no longer exposes smartApplyData")
    try:
        data = json.loads(node.get_text())
    except (TypeError, ValueError) as exc:
        raise ValueError("Netflix smartApplyData is not valid JSON") from exc
    if data.get("domain") != DOMAIN:
        raise ValueError("Netflix smartApplyData domain changed")
    branding = data.get("branding") or {}
    if branding.get("companyName") != EMPLOYER_NAME:
        raise ValueError("Netflix smartApplyData company identity changed")
    if data.get("isHomePageLoad") is not True or data.get("excludePrivatePositions") is not True:
        raise ValueError("Netflix listing is no longer the unfiltered public home listing")
    count = data.get("count")
    positions = data.get("positions")
    if not isinstance(count, int) or count <= 0:
        raise ValueError("Netflix listing count is missing or invalid")
    if not isinstance(positions, list) or not positions:
        raise ValueError("Netflix listing has no server-rendered positions")
    if len(positions) > count:
        raise ValueError("Netflix server-rendered positions exceed the advertised count")
    query = data.get("query") or {}
    for key in ("query", "location", "pid"):
        if query.get(key) not in (None, ""):
            raise ValueError(f"Netflix listing unexpectedly applied {key} filter")
    for key in ("department", "skill", "seniority", "Teams", "Work Type", "Region"):
        if query.get(key):
            raise ValueError(f"Netflix listing unexpectedly applied {key} filter")
    facets = data.get("facets") or {}
    for key in ("Teams", "Work Type", "Region"):
        values = facets.get(key)
        if not isinstance(values, dict) or sum(values.values()) != count:
            raise ValueError(f"Netflix unfiltered facet {key} no longer sums to the advertised count")
    rows = [_validate_listing_row(row, "server-rendered") for row in positions]
    ids = [str(row["id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Netflix server-rendered positions contain duplicate ids")
    sort_labels = [item.get("label") for item in data.get("sortByConfig") or [] if isinstance(item, dict)]
    if "Relevance" not in sort_labels:
        raise ValueError("Netflix default relevance sort is no longer published")
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical.get("href"),
        "html_lang": (soup.html or {}).get("lang") if soup.html else None,
        "domain": data.get("domain"),
        "company_name": branding.get("companyName"),
        "count": count,
        "positions": rows,
        "position_ids": ids,
        "facets": facets,
        "query": query,
        "sort_labels": sort_labels,
        "exclude_private_positions": data.get("excludePrivatePositions"),
        "is_home_page_load": data.get("isHomePageLoad"),
    }


def parse_listing_page(value: dict, requested_start: int, expected_count: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Netflix listing response is not an object")
    if value.get("domain") != DOMAIN:
        raise ValueError("Netflix listing response domain changed")
    count = value.get("count")
    positions = value.get("positions")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Netflix listing count is missing or invalid")
    if not isinstance(positions, list):
        raise ValueError("Netflix listing positions is missing")
    if expected_count is not None and count != expected_count:
        raise ValueError("Netflix listing count changed during enumeration")
    if requested_start < 0 or requested_start % PAGE_SIZE:
        raise ValueError("Netflix listing offset is not a UI page boundary")
    if len(positions) > PAGE_SIZE:
        raise ValueError("Netflix listing page exceeds the observed page size")
    if requested_start + len(positions) > count:
        raise ValueError("Netflix listing page extends past the advertised count")
    query = value.get("query") or {}
    for key in ("query", "location", "pid"):
        if query.get(key) not in (None, ""):
            raise ValueError(f"Netflix listing page unexpectedly applied {key} filter")
    for key in ("department", "skill", "seniority", "Teams", "Work Type", "Region"):
        if query.get(key):
            raise ValueError(f"Netflix listing page unexpectedly applied {key} filter")
    rows = [_validate_listing_row(row, f"page-{requested_start}") for row in positions]
    ids = [str(row["id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Netflix listing page contains duplicate ids")
    if requested_start + len(rows) < count and len(rows) != PAGE_SIZE:
        raise ValueError("Netflix listing returned a short page before the advertised end")
    return {
        "rows": rows,
        "count": count,
        "ids": ids,
        "start": requested_start,
        "page_size": len(rows),
    }


def parse_detail_api(value: dict, expected_row: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Netflix detail API response is not an object")
    identity = str(expected_row["id"])
    if str(value.get("id")) != identity:
        raise ValueError("Netflix detail API id differs from the listing id")
    if normalized_title(value.get("name")) != normalized_title(expected_row.get("name")):
        raise ValueError("Netflix detail API title differs from the listing title")
    if str(value.get("ats_job_id") or "") != str(expected_row.get("ats_job_id") or ""):
        raise ValueError("Netflix detail API requisition differs from the listing requisition")
    if value.get("isPrivate") is not False or value.get("type") != "ATS":
        raise ValueError("Netflix detail API no longer describes a public ATS posting")
    description = value.get("job_description")
    if not isinstance(description, str) or len(text_html(description).strip()) < 100:
        raise ValueError("Netflix detail API job_description is empty or too short")
    locations = value.get("locations")
    if not isinstance(locations, list) or not locations or not all(isinstance(x, str) and x.strip() for x in locations):
        raise ValueError("Netflix detail API has no locations")
    parsed = urlsplit(str(value.get("canonicalPositionUrl") or ""))
    if parsed.scheme != "https" or parsed.netloc != "explore.jobs.netflix.net" or parsed.path != _job_path(identity):
        raise ValueError("Netflix detail API canonical URL changed")
    custom = value.get("custom_JD") or {}
    fields = custom.get("data_fields") or {}
    posting = fields.get("posting_date") or []
    if posting and (not isinstance(posting, list) or not POSTING_DATE_RE.fullmatch(str(posting[0]))):
        raise ValueError("Netflix detail API Job Posting Date format changed")
    team = fields.get("team") or []
    work_type = fields.get("work_type") or []
    requisition_type = fields.get("requisition_type") or []
    if team and not all(isinstance(x, str) and x for x in team):
        raise ValueError("Netflix detail API team field changed")
    if work_type and not all(isinstance(x, str) and x for x in work_type):
        raise ValueError("Netflix detail API work_type field changed")
    if requisition_type and not all(isinstance(x, str) and x for x in requisition_type):
        raise ValueError("Netflix detail API requisition_type field changed")
    if not isinstance(value.get("urls"), dict):
        raise ValueError("Netflix detail API urls field changed")
    return {
        "id": identity,
        "title": normalized_title(value.get("name")),
        "requisition_id": str(value.get("ats_job_id")),
        "description_html": description,
        "description_text": text_html(description),
        "locations": list(locations),
        "department": value.get("department"),
        "business_unit": value.get("business_unit"),
        "locale": value.get("locale"),
        "t_create": value.get("t_create"),
        "t_update": value.get("t_update"),
        "posting_date": str(posting[0]) if posting else None,
        "team": str(team[0]) if team else None,
        "work_type": str(work_type[0]) if work_type else None,
        "requisition_type": str(requisition_type[0]) if requisition_type else None,
        "urls": value.get("urls"),
        "custom_JD": custom,
    }


def _schema_job_posting(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    schemas = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(node.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            schemas.append(value)
    if len(schemas) != 1:
        raise ValueError(f"Netflix detail page has {len(schemas)} JobPosting schemas")
    return schemas[0]


def _schema_locations(schema: dict) -> list[dict]:
    value = schema.get("jobLocation")
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def parse_detail_page(html: str, expected_row: dict, expected_url: str,
                      expected_description_html: str | None = None) -> dict:
    parsed = urlsplit(expected_url)
    identity = str(expected_row["id"])
    if (parsed.scheme != "https" or parsed.netloc != "explore.jobs.netflix.net"
            or parsed.path != _job_path(identity)):
        raise ValueError("Netflix detail page URL is not the exact exposed job URL")
    schema = _schema_job_posting(html)
    if normalized_title(schema.get("title")) != normalized_title(expected_row.get("name")):
        raise ValueError("Netflix detail page schema title differs from the listing title")
    organization = schema.get("hiringOrganization") or {}
    if organization.get("name") != EMPLOYER_NAME:
        raise ValueError("Netflix detail page hiring organization changed")
    date_posted = schema.get("datePosted")
    if not isinstance(date_posted, str) or not DATE_RE.fullmatch(date_posted):
        raise ValueError("Netflix detail page schema datePosted is missing or invalid")
    valid_through = schema.get("validThrough")
    if valid_through is not None and (not isinstance(valid_through, str) or not DATE_RE.fullmatch(valid_through)):
        raise ValueError("Netflix detail page schema validThrough format changed")
    employment_type = schema.get("employmentType")
    if not isinstance(employment_type, str) or not employment_type.strip():
        raise ValueError("Netflix detail page schema employmentType is missing")
    schema_url = urlsplit(str(schema.get("url") or ""))
    if (schema_url.scheme != "https" or schema_url.netloc != "explore.jobs.netflix.net"
            or not JOB_DETAIL_PATH.fullmatch(schema_url.path) or JOB_DETAIL_PATH.fullmatch(schema_url.path).group(1) != identity):
        raise ValueError("Netflix detail page schema URL is not the requested posting")
    schema_query = parse_qs(schema_url.query)
    if schema_query.get("domain") != [DOMAIN] or schema_query.get("microsite") != [DOMAIN]:
        raise ValueError("Netflix detail page schema URL domain changed")
    description = schema.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Netflix detail page schema description is empty")
    locations = _schema_locations(schema)
    if len(locations) != len(expected_row.get("locations") or []):
        raise ValueError("Netflix detail page schema location count differs from the listing")
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("address"), dict):
            raise ValueError("Netflix detail page schema location structure changed")
    if schema.get("inLanguage") not in (None, "en-US"):
        raise ValueError("Netflix detail page schema language changed")
    description_text = normalized_plain_text(description)
    equivalent = None
    if expected_description_html is not None:
        api_text = normalized_body_text(expected_description_html)
        equivalent = comparison_key(api_text) == comparison_key(description_text)
        if not equivalent:
            raise ValueError("Netflix detail page JSON-LD body is not text-equivalent to the API body")
    return {
        "title": normalized_title(schema.get("title")),
        "date_posted": date_posted[:10],
        "date_posted_raw": date_posted,
        "valid_through": valid_through,
        "employment_type": employment_type,
        "organization": organization,
        "job_location": locations,
        "job_location_type": schema.get("jobLocationType"),
        "applicant_location_requirements": schema.get("applicantLocationRequirements"),
        "url": str(schema.get("url")),
        "in_language": schema.get("inLanguage"),
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_sha256": _sha256(description_text),
        "description_equivalent_to_api": equivalent,
        "schema": schema,
    }


def heading_kind(node: Tag) -> str | None:
    """Classify a top-level block as a major heading, a sub-heading, or content."""
    text = normalized_title(node.get_text(" ", strip=True))
    if not text or len(text) > 120:
        return None
    low = text.lower().rstrip(":").strip()
    major = low.startswith(MAJOR_HEADINGS)
    if node.name in ("h1", "h2"):
        return "major"
    if node.name in ("h3", "h4", "h5", "h6"):
        return "major" if (major or (text.isupper() and len(text) <= 80)) else "sub"
    if text.isupper() and len(text) <= 80:
        return "major"
    if major:
        return "major"
    if node.name == "p":
        strong = node.find(["b", "strong"])
        if strong and normalized_title(strong.get_text(" ", strip=True)) == text:
            return "sub"
    return None


def heading_category(heading: str | None) -> str | None:
    if not heading or "application" in heading.lower():
        return None
    if RESPONSIBILITY_HEADING.search(heading):
        return "responsibility"
    if QUALIFICATION_HEADING.search(heading):
        return "qualification"
    return None


def description_sections(html: str | None) -> list[dict]:
    """Keep employer-authored sections, including nested bold sub-headings."""
    soup = BeautifulSoup(f'<div data-netflix-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-netflix-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        if block.name in ("br", "hr"):
            continue
        kind = heading_kind(block)
        if kind == "major":
            if current is not None:
                sections.append(current)
            current = {"heading": normalized_title(block.get_text(" ", strip=True)),
                       "subheadings": [], "html": "", "items": [], "text": ""}
            continue
        if current is None:
            current = {"heading": None, "subheadings": [], "html": "", "items": [], "text": ""}
        if kind == "sub":
            current["subheadings"].append(normalized_title(block.get_text(" ", strip=True)))
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        fragment = BeautifulSoup(section["html"], "lxml")
        list_items = fragment.find_all("li")
        if list_items:
            section["items"] = [normalized_title(item.get_text(" ", strip=True)) for item in list_items]
        else:
            section["items"] = [
                normalized_title(paragraph.get_text(" ", strip=True))
                for paragraph in fragment.find_all("p")
                if normalized_title(paragraph.get_text(" ", strip=True))
            ]
        section["text"] = normalized_title(fragment.get_text(" ", strip=True))
        section.pop("html", None)
    # A heading such as "REQUIREMENTS" is sometimes followed immediately by an
    # introducing phrase ("The successful candidate will have:") before the
    # list.  Merge that empty heading into the following same-category section.
    merged: list[dict] = []
    index = 0
    while index < len(sections):
        section = sections[index]
        if (not section["items"] and index + 1 < len(sections)
                and heading_category(section["heading"]) is not None
                and heading_category(section["heading"]) == heading_category(sections[index + 1]["heading"])):
            following = sections[index + 1]
            following["subheadings"] = (
                section["subheadings"]
                + ([following["heading"]] if following["heading"] and following["heading"] != section["heading"] else [])
                + following["subheadings"]
            )
            following["heading"] = section["heading"]
            merged.append(following)
            index += 2
            continue
        merged.append(section)
        index += 1
    return merged


def _section_matches(section: dict, pattern: re.Pattern) -> bool:
    heading = section.get("heading")
    if not heading:
        return False
    if "application" in heading.lower():
        return False
    return bool(pattern.search(heading))


def section_fields(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    responsibilities = [
        {"heading": section["heading"], "subheadings": section["subheadings"], "items": section["items"]}
        for section in sections
        if _section_matches(section, RESPONSIBILITY_HEADING) and section["items"]
    ]
    qualifications = [
        {"heading": section["heading"], "subheadings": section["subheadings"], "items": section["items"]}
        for section in sections
        if _section_matches(section, QUALIFICATION_HEADING) and section["items"]
    ]
    return responsibilities, qualifications


def _amount(value: str, suffix: str | None) -> tuple[float, list[str]]:
    notes = []
    text = value.strip().replace(" ", "")
    if "," in text and "." not in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:
            number = float(text.replace(",", "."))
        else:
            number = float(text.replace(",", ""))
    else:
        number = float(text.replace(",", ""))
    if suffix and suffix.lower() == "k":
        if number < 1000:
            number *= 1000
        else:
            notes.append("k_suffix_on_full_amount")
    return round(number, 2), notes


def _infer_currency(locations) -> str | None:
    values = [str(location).lower() for location in locations or []]
    if not values:
        return None
    if all("united states" in value for value in values):
        return "USD"
    if all("canada" in value for value in values):
        return "CAD"
    if all("united kingdom" in value for value in values):
        return "GBP"
    return None


def _salary_result(window: str, locations, *, basis: str, period: str,
                   low: float, high: float | None, explicit_currency: str | None,
                   notes: list[str]) -> dict:
    currency = explicit_currency or _infer_currency(locations)
    return {
        "min": low,
        "max": high,
        "currency": currency,
        "currency_source": "description" if explicit_currency else ("location_inference" if currency else None),
        "period": period,
        "basis": basis,
        "raw_text": normalized_plain_text(window),
        "notes": list(dict.fromkeys(notes)),
    }


def parse_salary(description_text: str | None, locations=None) -> dict | None:
    """Extract only pay ranges that the employer publishes in the body prose."""
    text = normalized_plain_text(description_text)
    if not text:
        return None
    for match in re.finditer(r"\brange\b", text, re.I):
        window = text[match.start():match.start() + 500]
        boundary = re.search(r"\.(?:\s|$)", window)
        if boundary:
            window = window[:boundary.start() + 1]
        if "$" not in window:
            continue
        range_match = SALARY_RANGE.search(window)
        if range_match and range_match.start() <= 160:
            low, notes = _amount(range_match.group("min"), range_match.group("mink"))
            high, high_notes = _amount(range_match.group("max"), range_match.group("maxk"))
            notes.extend(high_notes)
            period = "hour" if re.search(r"hour|/\s*hr|an hour", window, re.I) else "year"
            return _salary_result(
                window, locations, basis="hourly_range" if period == "hour" else "salary_range",
                period=period, low=low, high=high,
                explicit_currency=(range_match.group("currency") or "").upper() or None,
                notes=notes,
            )
        single_match = SALARY_SINGLE.search(window)
        if single_match and single_match.start() <= 160:
            low, notes = _amount(single_match.group("amount"), single_match.group("k"))
            period = "hour" if re.search(r"hour|/\s*hr|an hour", window, re.I) else "year"
            currency_match = re.search(r"\b(USD|CAD|EUR|GBP)\b", window, re.I)
            return _salary_result(
                window, locations, basis="hourly_rate" if period == "hour" else "salary_single",
                period=period, low=low, high=low,
                explicit_currency=currency_match.group(1).upper() if currency_match else None,
                notes=notes,
            )
    cba = CBA_RATE.search(text)
    if cba:
        low, notes = _amount(cba.group("amount"), cba.group("k"))
        return _salary_result(
            cba.group(0), locations, basis="collective_bargaining_minimum", period="hour",
            low=low, high=None, explicit_currency=None,
            notes=notes + ["collective_bargaining_minimum"],
        )
    return None


def posting_classification(title: str) -> str:
    if EXPRESSION_OF_INTEREST_RE.search(title):
        return "expression_of_interest"
    if INTERNSHIP_RE.search(title):
        return "internship"
    if EARLY_CAREER_RE.search(title):
        return "early_career"
    return "posting"


def description_links(html: str | None) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    return [
        {"text": normalized_title(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
        for anchor in soup.select("a[href]")
    ]


class Netflix(Adapter):
    """The exact Netflix careers listing and detail flow."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.challenge: str | None = None

    async def _fetch(self, url: str, **kwargs):
        try:
            return await self.http.get(url, **kwargs)
        except FetchError as exc:
            message = str(exc)
            if re.search(r"HTTP (?:403|429)\b", message) or "captcha" in message.lower():
                self.challenge = message
                raise SourceChallenge(message) from exc
            raise

    async def _json(self, url: str, **kwargs):
        response = await self._fetch(url, **kwargs)
        try:
            return response, response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                self.challenge = f"HTML challenge instead of JSON from {response.url}"
                raise SourceChallenge(self.challenge) from exc
            raise FetchError(f"Expected JSON from {response.url}: {exc}") from exc

    async def _listing(self, expected_count: int, *, initial: bool) -> dict:
        started = time.perf_counter()
        rows, pages, seen = [], [], set()
        page_by_id = {}
        offset = 0
        while offset < expected_count:
            if initial and not self.page_guard():
                raise ValueError("Netflix listing page budget exhausted")
            params = {"domain": DOMAIN, "start": offset, "num": PAGE_SIZE, "sort_by": SORT_BY}
            response, value = await self._json(LISTING_API, params=params)
            page = parse_listing_page(value, offset, expected_count)
            page["request_url"] = str(response.request.url)
            page["final_url"] = str(response.url)
            page["response_bytes"] = len(response.content)
            page["response_sha256"] = _sha256(response.content)
            page["request_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            pages.append(page)
            for row in page["rows"]:
                identity = str(row["id"])
                if identity in seen:
                    raise ValueError(f"Netflix listing repeated id {identity}")
                seen.add(identity)
                page_by_id[identity] = {
                    "start": offset,
                    "response_sha256": page["response_sha256"],
                }
                rows.append(row)
            if not page["rows"]:
                raise ValueError("Netflix listing returned an empty page before the advertised end")
            offset += len(page["rows"])
        if len(rows) != expected_count:
            raise ValueError(f"Netflix listing collected {len(rows)} rows for advertised {expected_count}")
        ids = [str(row["id"]) for row in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("Netflix listing membership contains duplicate ids")
        return {
            "rows": rows,
            "ids": ids,
            "count": expected_count,
            "pages": pages,
            "page_by_id": page_by_id,
            "page_count": len(pages),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
            "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
        }

    async def _detail(self, job):
        if self.challenge:
            raise SourceChallenge(self.challenge)
        started = time.perf_counter()
        listing = job.raw["listing"]
        api_url = DETAIL_API.format(job_id=job.job_id)
        api_response, api_value = await self._json(api_url, params={"domain": DOMAIN})
        api = parse_detail_api(api_value, listing)
        page_url = JOB_URL.format(job_id=job.job_id)
        page_response = await self._fetch(page_url)
        page = parse_detail_page(
            page_response.text, listing, page_url,
            expected_description_html=api["description_html"],
        )
        description_html = api["description_html"]
        description_text = normalized_body_text(description_html)
        sections = description_sections(description_html)
        responsibilities, qualifications = section_fields(sections)
        salary = parse_salary(description_text, api["locations"])
        links = description_links(description_html)
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        job.title = api["title"]
        job.url = page_url
        job.job_id = api["id"]
        job.requisition_id = api["requisition_id"]
        job.apply_url = APPLY_URL.format(job_id=api["id"])
        job.description_html = description_html
        job.description_text = description_text
        job.description_method = "netflix.eightfold.apply_v2.job_description"
        job.description_source_url = api_url
        job.locations = api["locations"]
        job.department = api["department"]
        job.team = api["team"]
        job.employment_type = page["employment_type"]
        job.workplace_type = api["work_type"]
        job.date_posted = page["date_posted"]
        job.date_posted_raw = {
            "jsonld_datePosted": page["date_posted_raw"],
            "custom_JD_posting_date": api["posting_date"],
            "t_create": api["t_create"],
            "t_update": api["t_update"],
        }
        job.date_updated = None
        job.valid_through = None
        job.salary = salary
        job.qualifications = qualifications or None
        job.responsibilities = responsibilities or None
        job.employer = EMPLOYER_NAME
        job.language = api["locale"] or "en-US"
        job.description_verified = False
        job.raw["detail"] = {
            "api": api_value,
            "api_url": api_url,
            "api_response_sha256": _sha256(api_response.content),
            "api_response_bytes": len(api_response.content),
            "page_url": page_url,
            "page_response_sha256": _sha256(page_response.content),
            "page_response_bytes": len(page_response.content),
            "page_title": normalized_title(BeautifulSoup(page_response.text, "lxml").title.get_text(" ", strip=True)
                                           if BeautifulSoup(page_response.text, "lxml").title else ""),
            "jsonld": {
                "datePosted": page["date_posted_raw"],
                "validThrough": page["valid_through"],
                "employmentType": page["employment_type"],
                "hiringOrganization": page["organization"],
                "jobLocation": page["job_location"],
                "jobLocationType": page["job_location_type"],
                "applicantLocationRequirements": page["applicant_location_requirements"],
                "url": page["url"],
                "inLanguage": page["in_language"],
                "description_sha256": page["description_sha256"],
                "description_normalized_chars": page["description_normalized_chars"],
                "description_fnv1a64": page["description_fnv1a64"],
                "description_equivalent_to_api": page["description_equivalent_to_api"],
            },
            "dates": {
                "date_posted_field_used": "detail page JSON-LD datePosted",
                "date_posted_iso": page["date_posted"],
                "date_posted_raw": page["date_posted_raw"],
                "custom_JD_posting_date": api["posting_date"],
                "t_create_epoch": api["t_create"],
                "t_update_epoch": api["t_update"],
                "t_create_iso": epoch_iso(api["t_create"]),
                "t_update_iso": epoch_iso(api["t_update"]),
                "t_update_used_as_updated_date": False,
                "validThrough_raw": page["valid_through"],
                "validThrough_derivation": "Eightfold-generated datePosted + 180 days; retained only as source metadata, not as a company deadline",
                "date_updated": None,
                "valid_through": None,
            },
            "description_sha256": _sha256(description_html),
            "description_normalized_chars": len(description_text),
            "description_fnv1a64": fnv1a64(description_text),
            "description_sections": sections,
            "description_links": links,
            "role_attachments": attachments,
            "salary": salary,
            "posting_classification": posting_classification(api["title"]),
            "application_route": {
                "listing_url": listing["canonicalPositionUrl"],
                "detail_page_url": page_url,
                "detail_api_url": api_url,
                "apply_url": job.apply_url,
                "apply_button_label_observed": "APPLY NOW",
                "apply_url_pattern_observed": "/careers/apply?pid={id}",
                "application_form_questions_excluded": True,
            },
            "listing_page": self.listing_page_by_id.get(str(job.job_id), {}),
        }
        job.raw["detail"]["detail_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return job

    async def run(self):
        run_started = time.perf_counter()
        official_started = time.perf_counter()
        response = await self._fetch(OFFICIAL_ENTRY)
        official = parse_official_entry(response.text, str(response.url))
        self.result.metadata["official_entry"] = {
            **{key: official[key] for key in (
                "url", "title", "canonical", "html_lang", "domain", "company_name",
                "count", "position_ids", "facets", "query", "sort_labels",
                "exclude_private_positions", "is_home_page_load",
            )},
            "final_url": str(response.url),
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": _sha256(response.content),
        }
        self.result.expected_total = official["count"]
        self.result.timings["official_entry_seconds"] = round(max(0.0, time.perf_counter() - official_started), 6)

        initial = await self._listing(official["count"], initial=True)
        self.listing_page_by_id = initial["page_by_id"]
        self.result.metadata["listing"] = {
            "url": LISTING_API,
            "count": initial["count"],
            "page_size": PAGE_SIZE,
            "sort_by": SORT_BY,
            "pages": initial["page_count"],
            "ids": initial["ids"],
            "first_id": initial["first_id"],
            "last_id": initial["last_id"],
            "page_records": [
                {key: page[key] for key in (
                    "start", "page_size", "count", "ids", "request_url", "final_url",
                    "response_bytes", "response_sha256",
                )}
                for page in initial["pages"]
            ],
            "server_rendered_ids": official["position_ids"],
            "server_rendered_ids_match_api_first_page": official["position_ids"] == initial["ids"][:len(official["position_ids"])],
            "server_rendered_titles_match_api_first_page": all(
                normalized_title(position.get("name")) == normalized_title(row.get("name"))
                for position, row in zip(official["positions"], initial["rows"])
            ),
        }
        self.result.timings["listing_initial_seconds"] = initial["seconds"]
        if not self.result.metadata["listing"]["server_rendered_ids_match_api_first_page"]:
            self.result.errors.append("netflix_server_rendered_membership_differs_from_api")

        for row in initial["rows"]:
            self.add(self.job(
                {"listing": row},
                title=row["name"],
                url=JOB_URL.format(job_id=row["id"]),
                job_id=str(row["id"]),
                requisition_id=row["ats_job_id"],
                apply_url=APPLY_URL.format(job_id=row["id"]),
                locations=list(row["locations"]),
                department=row["department"],
                workplace_type=row.get("work_location_option"),
                employer=EMPLOYER_NAME,
                language="en-US",
                date_posted=None,
                date_updated=None,
                valid_through=None,
            ))
            if self.limit():
                break

        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)

        final_started = time.perf_counter()
        try:
            final = await self._listing(official["count"], initial=False)
        except Exception as exc:
            final = None
            self.result.errors.append(f"netflix_final_listing_recheck_failed:{type(exc).__name__}:{exc}")
            self.result.listing_complete = False
        self.result.timings["listing_final_recheck_seconds"] = round(max(0.0, time.perf_counter() - final_started), 6)
        if final is not None:
            self.result.metadata["final_listing_recheck"] = {
                "count": final["count"],
                "pages": final["page_count"],
                "ids": final["ids"],
                "first_id": final["first_id"],
                "last_id": final["last_id"],
                "seconds": final["seconds"],
            }
        initial_ids = initial["ids"]
        final_ids = final["ids"] if final is not None else []
        complete = (
            final is not None
            and initial["count"] == final["count"] == official["count"] == len(self.result.jobs)
            and initial_ids == final_ids
            and not self.result.errors
        )
        self.result.listing_complete = complete
        if not complete:
            if final is not None and initial_ids != final_ids:
                self.result.errors.append("netflix_membership_changed_during_collection")
            elif final is not None and initial["count"] != final["count"]:
                self.result.errors.append("netflix_listing_count_changed_during_collection")
            else:
                self.result.errors.append("netflix_membership_not_reconciled")
        self.result.metadata["membership_reconciliation"] = {
            "initial_count": initial["count"],
            "final_count": final["count"] if final is not None else None,
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "initial_pages": initial["page_count"],
            "final_pages": final["page_count"] if final is not None else None,
            "collected_jobs": len(self.result.jobs),
            "complete": complete,
        }
        self.result.metadata["scope"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "domain": DOMAIN,
            "company_name": official["company_name"],
            "listing_microsite": "explore.jobs.netflix.net/careers",
            "excluded_linked_microsite": {
                "url": "https://apply.netflixhouse.com/careers",
                "reason": "Separate Netflix House Eightfold microsite linked from the navigation; its 19 roles are not part of this company-scoped listing and are not mixed into its membership.",
            },
        }
        details = self.result.metadata.get("details", {})
        details.update({
            "jsonld_verified_jobs": sum(1 for job in self.result.jobs if job.raw.get("detail", {}).get("jsonld")),
            "description_text_equivalent_jobs": sum(
                1 for job in self.result.jobs
                if job.raw.get("detail", {}).get("jsonld", {}).get("description_equivalent_to_api") is True
            ),
            "salary_parsed_jobs": sum(1 for job in self.result.jobs if job.salary),
            "responsibility_section_jobs": sum(1 for job in self.result.jobs if job.responsibilities),
            "qualification_section_jobs": sum(1 for job in self.result.jobs if job.qualifications),
        })
        self.result.metadata["details"] = details
        self.result.timings["run_wall_seconds"] = round(max(0.0, time.perf_counter() - run_started), 6)
        return self.result
