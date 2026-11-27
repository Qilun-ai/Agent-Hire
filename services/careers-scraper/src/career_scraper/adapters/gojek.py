"""Gojek's company-owned Next.js careers listing and native detail pages.

The official careers page publishes a 12-row React Server Component (RSC)
dataset and renders ten of those rows per UI page.  The listing is paginated
client-side; the server also accepts ``?p=2`` and renders the second slice.  The
page's rows point at Lever apply URLs under the shared ``GoToGroup`` account, but
the only membership enumerated here is the exact ``openPositions`` array the
Gojek page itself publishes.  GoTo Group and GoTo Financial have separate
careers routes and are not counted as Gojek.

A plain HTTP request to the site currently receives Vercel's 429 security
checkpoint.  The runner records that block and reuses a headless Chromium
rendered capture of the official pages.  No CAPTCHA or challenge token is
solved or forged.

The Lever ``additional`` field mixes the role's "About the Team" block with
GoTo Group's corporate boilerplate (About GoTo Group / About Gojek / About GoTo
Financial, the shared job-scam notice and the ``#LI-*`` tag).  That generic tail
is split off at the first generic heading and preserved once in
``raw.detail.generic_company_sections`` instead of being counted as the role
description.  Sections present in the RSC row but not rendered by the live
detail page are preserved in ``raw.detail.unrendered_sections`` and kept out of
the main description, so the stored role body equals the visible role body.
"""
from __future__ import annotations

from copy import copy
from difflib import SequenceMatcher
from hashlib import sha256
from html import escape
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


ENTRY_URL = "https://www.gojek.io/careers"
PAGE_SIZE = 10
PAGE_QUERY = "p"
PUBLISHER = "Gojek Tech"
GO_TO_GROUP_CAREERS = "https://www.gotocompany.com/en/careers"
GENERIC_SECTION_LABELS = ("About GoTo Group", "About Gojek", "About GoTo Financial")
GENERIC_NOTICE_PREFIX = "GoTo and its business units"
RECRUITER_TAG = re.compile(r"#LI-[A-Z]+")
HEADING_TAGS = ("b", "strong", "h1", "h2", "h3", "h4")
RSC_PUSH = re.compile(r"self\.__next_f\.push\(")
RSC_HEADER = re.compile(rb"([0-9a-f]*):")
RSC_TEXT_LENGTH = re.compile(rb"^T([0-9a-f]+)$")
RSC_REFERENCE = re.compile(r"^\$([0-9a-f]+)$")
RSC_CONTROL_VALUES = {"X", "C", "300"}
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
SEARCH_COUNT = re.compile(r"\b(\d+)\s+open jobs found\b", re.I)
ROLE_LABELS_RESPONSIBILITY = ("what you will do", "responsibilities", "what you'll do")
ROLE_LABELS_QUALIFICATION = (
    "what you will need", "qualification", "nice to have", "bonus point", "requirements",
)


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u200b", " ")).strip()


def fnv1a64(value: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def decode_rsc_records(html: str | bytes):
    """Decode the page's published RSC stream without evaluating JavaScript."""
    soup = BeautifulSoup(html, "lxml")
    decoder = json.JSONDecoder()
    fragments = []
    for script in soup.find_all("script"):
        text = script.get_text()
        for match in RSC_PUSH.finditer(text):
            try:
                value, _end = decoder.raw_decode(text[match.end():])
            except ValueError:
                continue
            if (isinstance(value, list) and len(value) == 2 and value[0] == 1
                    and isinstance(value[1], str)):
                fragments.append(value[1])
    if not fragments:
        raise ValueError("Gojek official page has no Next RSC data")

    stream = "".join(fragments).encode("utf-8")
    records, conflicts, cursor = {}, [], 0
    while cursor < len(stream):
        header = RSC_HEADER.match(stream, cursor)
        if header is None:
            raise ValueError(f"Gojek RSC record boundary changed at byte {cursor}")
        record_id = header.group(1).decode("ascii")
        cursor = header.end()
        if stream[cursor:cursor + 1] == b"T":
            comma = stream.find(b",", cursor)
            if comma < 0:
                raise ValueError("Gojek RSC text record has no length delimiter")
            size_match = RSC_TEXT_LENGTH.fullmatch(stream[cursor:comma])
            if size_match is None:
                raise ValueError("Gojek RSC text record length is invalid")
            size = int(size_match.group(1), 16)
            cursor = comma + 1
            raw = stream[cursor:cursor + size]
            if len(raw) != size:
                raise ValueError(f"Gojek RSC text record {record_id} is truncated")
            value = raw.decode("utf-8")
            cursor += size
        else:
            end = stream.find(b"\n", cursor)
            if end < 0:
                end = len(stream)
                raw = stream[cursor:end].decode("utf-8")
                cursor = end
            else:
                raw = stream[cursor:end].decode("utf-8")
                cursor = end + 1
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
        if not record_id:
            continue
        if record_id in records and records[record_id] != value:
            previous = records[record_id]
            # The page emits short Next control-record replacements for
            # placeholder/stream markers.  Data-bearing references are checked
            # separately and cannot use these control values.
            if previous in RSC_CONTROL_VALUES or value in RSC_CONTROL_VALUES:
                conflicts.append(
                    {"record_id": record_id, "previous": previous, "replacement": value}
                )
                records[record_id] = value
            else:
                raise ValueError(f"Gojek RSC data record {record_id} conflicts")
        else:
            records[record_id] = value
    return records, conflicts


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def resolve_rsc(value, records, active=()):
    if isinstance(value, str):
        reference = RSC_REFERENCE.fullmatch(value)
        if not reference or reference.group(1) not in records:
            return value
        record_id = reference.group(1)
        if record_id in active:
            raise ValueError("Gojek RSC reference cycle at $" + record_id)
        return resolve_rsc(records[record_id], records, (*active, record_id))
    if isinstance(value, list):
        return [resolve_rsc(item, records, active) for item in value]
    if isinstance(value, dict):
        return {key: resolve_rsc(item, records, active) for key, item in value.items()}
    return value


def _is_posting_array(value):
    required = {
        "id", "text", "categories", "description", "lists", "hostedUrl", "applyUrl",
    }
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(row, dict) and required <= set(row) for row in value)
    )


def native_posting_rows(records):
    candidates = {}
    for value in records.values():
        for item in _walk(value):
            if isinstance(item, dict) and _is_posting_array(item.get("data")):
                signature = tuple(str(row["id"]) for row in item["data"])
                candidates[signature] = item["data"]
    if len(candidates) != 1:
        raise ValueError(f"Gojek RSC page has {len(candidates)} unique posting datasets")
    raw_rows = next(iter(candidates.values()))
    identities = [str(row["id"]) for row in raw_rows]
    if len(set(identities)) != len(identities):
        raise ValueError("Gojek RSC page repeats a native job UUID")
    for identity, row in zip(identities, raw_rows):
        if not UUID_PATTERN.fullmatch(identity) or str(UUID(identity)) != identity:
            raise ValueError(f"Gojek native posting ID is not a canonical UUID: {identity}")
        if not isinstance(row.get("text"), str) or not normalized_text(row.get("text")):
            raise ValueError(f"Gojek native posting {identity} has no exact title")
        if not isinstance(row.get("categories"), dict):
            raise ValueError(f"Gojek native posting {identity} has no categories")
        if not isinstance(row.get("lists"), list):
            raise ValueError(f"Gojek native posting {identity} has no section list")
    return raw_rows


def native_detail_links(soup, page_url):
    links, labels = {}, {}
    for anchor in soup.select('a[href*="/careers/view/"]'):
        href = urljoin(page_url, anchor.get("href", ""))
        parsed = urlsplit(href)
        parts = parsed.path.rstrip("/").split("/")
        if (parsed.scheme != "https" or parsed.hostname != "www.gojek.io"
                or len(parts) != 5 or parts[1:3] != ["careers", "view"]):
            continue
        identity = parts[4]
        if not UUID_PATTERN.fullmatch(identity) or str(UUID(identity)) != identity:
            raise ValueError("Gojek native card has an invalid UUID detail link")
        if parsed.query or parsed.fragment:
            raise ValueError("Gojek native detail link unexpectedly has a query or fragment")
        if identity in links and links[identity] != href:
            raise ValueError(f"Gojek native UUID {identity} has conflicting detail URLs")
        links[identity] = href
        labels.setdefault(identity, []).append(anchor.get_text(" ", strip=True))
    return links, labels


def page_number(url):
    parsed = urlsplit(str(url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {PAGE_QUERY}:
        raise ValueError("Gojek listing URL contains unexpected filters")
    values = query.get(PAGE_QUERY, [])
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError("Gojek page selector p is not a single integer")
    page = int(values[0])
    if page < 1:
        raise ValueError("Gojek UI page selector p must be one-based")
    return page


def _section_label(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "lxml")
    for node in soup.find_all(["strong", "b", "h1", "h2", "h3", "h4"]):
        label = normalized_text(node.get_text(" ", strip=True))
        if label and len(label) <= 80:
            return label
    first = normalized_text(text_html(value)).split("\n", 1)[0]
    return first[:80] or None


def is_generic_section(value: str | None) -> bool:
    text = normalized_text(text_html(value))
    return any(text.casefold().startswith(label.casefold()) for label in GENERIC_SECTION_LABELS)


def split_generic_additional(value: str | None) -> tuple[str, str]:
    """Split the mixed Lever ``additional`` field into role HTML and generic HTML.

    The generic corporate tail starts at the first top-level block that carries
    one of the observed generic headings (About GoTo Group / About Gojek / About
    GoTo Financial).  Everything before it is role content (normally "About the
    Team"); everything from it onwards is shared GoTo page furniture: the three
    corporate blurbs, the job-scam notice and the ``#LI-*`` recruiter tag.
    """
    if not isinstance(value, str) or not value.strip():
        return "", ""
    soup = BeautifulSoup(value, "lxml")
    body = soup.body or soup
    roots = [node for node in body.contents if getattr(node, "name", None)]
    for index, node in enumerate(roots):
        labels = [
            normalized_text(heading.get_text(" ", strip=True))
            for heading in node.find_all(list(HEADING_TAGS))
        ]
        if not any(label in GENERIC_SECTION_LABELS for label in labels):
            continue
        node_text = normalized_text(node.get_text(" ", strip=True))
        if not any(node_text.startswith(label) for label in GENERIC_SECTION_LABELS):
            raise ValueError(
                "Gojek additional field starts its generic block inside role content"
            )
        role_html = "".join(str(item) for item in roots[:index])
        generic_html = "".join(str(item) for item in roots[index:])
        return role_html, generic_html
    return value, ""


def generic_block_label(node) -> str | None:
    for heading in node.find_all(list(HEADING_TAGS)):
        label = normalized_text(heading.get_text(" ", strip=True))
        if label in GENERIC_SECTION_LABELS:
            return label
    return None


def generic_company_sections(row) -> dict:
    """Return the shared GoTo corporate blocks split out of ``additional``.

    The blocks are stored once, in published order, with their own HTML and
    normalised text; the normalised concatenation is what the live detail page
    renders after the role sections.
    """
    _role_html, generic_html = split_generic_additional(row.get("additional"))
    empty = {
        "blocks": [],
        "normalized_text": "",
        "normalized_chars": 0,
        "sha256": None,
        "source_field": "additional",
    }
    if not generic_html.strip():
        return empty
    soup = BeautifulSoup(generic_html, "lxml")
    body = soup.body or soup
    collected = []
    current = None
    for node in body.contents:
        if not getattr(node, "name", None):
            continue
        label = generic_block_label(node)
        text = normalized_text(node.get_text(" ", strip=True))
        if label:
            current = {"label": label, "html_parts": [str(node)], "text_parts": [text]}
            collected.append(current)
        elif text.startswith(GENERIC_NOTICE_PREFIX):
            current = {
                "label": "GoTo job posting notice",
                "html_parts": [str(node)],
                "text_parts": [text],
            }
            collected.append(current)
        elif RECRUITER_TAG.fullmatch(text):
            current = {"label": text, "html_parts": [str(node)], "text_parts": [text]}
            collected.append(current)
        elif current is not None:
            current["html_parts"].append(str(node))
            if text:
                current["text_parts"].append(text)
    blocks = [
        {
            "label": item["label"],
            "text": normalized_text(" ".join(item["text_parts"])),
            "html": "".join(item["html_parts"]),
        }
        for item in collected
        if normalized_text(" ".join(item["text_parts"]))
    ]
    text = normalized_text(" ".join(block["text"] for block in blocks))
    return {
        "blocks": blocks,
        "normalized_text": text,
        "normalized_chars": len(text),
        "sha256": sha256(text.encode()).hexdigest() if text else None,
        "source_field": "additional",
    }


def role_sections(row):
    """Return role-specific ``(label, html)`` sections in their published order.

    The mixed ``additional`` field is split first: only its role part (normally
    "About the Team") belongs here.  The generic GoTo corporate tail is preserved
    once by :func:`generic_company_sections` and never counted as the role.
    """
    sections = []
    if row.get("description"):
        sections.append((_section_label(row["description"]) or "About the Role",
                         row["description"]))
    for section in row.get("lists") or []:
        label = normalized_text(section.get("text"))
        content = section.get("content")
        if label and isinstance(content, str) and content.strip():
            sections.append((label, content))
    role_additional, _generic = split_generic_additional(row.get("additional"))
    if role_additional.strip():
        sections.append((_section_label(role_additional) or "About the Team",
                         role_additional))

    unique, seen = [], set()
    for label, content in sections:
        key = normalized_text(text_html(content))
        if key and key not in seen:
            seen.add(key)
            unique.append((label, content))
    return unique


def build_description_html(sections):
    parts = []
    for label, content in sections:
        body_text = normalized_text(text_html(content))
        label_text = normalized_text(label)
        if label_text and body_text.casefold().startswith(label_text.casefold()):
            # The description/additional fields carry their own visible heading.
            parts.append(content)
        else:
            parts.append(f"<section><h2>{escape(label)}</h2>{content}</section>")
    return "\n\n".join(parts)


def parse_listing_page(html, url, expected_page):
    """Parse the exact official listing response for one UI page.

    ``expected_page`` is validated through the rendered card slice, not through
    the URL alone: the client-side page-2 view keeps the ``/careers`` URL.  A
    plain HTTP ``?p=2`` response is also accepted and must carry the same slice.
    """
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.hostname != "www.gojek.io"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Gojek listing left the official /careers page")
    query_page = page_number(str(url))
    if expected_page == 1 and query_page not in (None, 1):
        raise ValueError(f"Gojek page selector p={query_page}, expected 1")
    if expected_page > 1 and query_page not in (None, expected_page):
        raise ValueError(f"Gojek page selector p={query_page}, expected {expected_page}")

    html_bytes = html.encode("utf-8") if isinstance(html, str) else html
    soup = BeautifulSoup(html_bytes, "lxml")
    count_node = soup.select_one(".job-results-text")
    match = SEARCH_COUNT.search(
        normalized_text(count_node.get_text(" ", strip=True) if count_node else
                       soup.get_text(" ", strip=True))
    )
    if not match:
        raise ValueError("Gojek official listing has no visible result count")
    total = int(match.group(1))
    keyword = soup.select_one('input#keyword[name="keyword"]')
    locations = soup.select_one('input[name="selectedLocations"]')
    if keyword is None or keyword.get("value", "") or locations is None or locations.get("value", ""):
        raise ValueError("Gojek search is not in the observed blank Keyword/Location state")

    records, conflicts = decode_rsc_records(html_bytes)
    raw_rows = native_posting_rows(records)
    rows = [resolve_rsc(row, records) for row in raw_rows]
    if len(rows) != total:
        raise ValueError(f"Gojek embedded {len(rows)} jobs but the page displays {total}")
    expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    slice_start = (expected_page - 1) * PAGE_SIZE
    expected_visible = [
        str(row["id"]) for row in rows[slice_start: slice_start + PAGE_SIZE]
    ]
    links, labels = native_detail_links(soup, str(url))
    visible_ids = list(links)
    if visible_ids != expected_visible:
        raise ValueError(f"Gojek page {expected_page} cards differ from the embedded RSC slice")
    page_buttons = sorted({
        int(button.get_text(" ", strip=True))
        for button in soup.select("#job-list button")
        if button.get_text(" ", strip=True).isdigit()
    })
    if page_buttons != list(range(1, expected_pages + 1)):
        raise ValueError("Gojek visible numbered pages do not match the displayed count")

    row_by_id = {str(row["id"]): row for row in rows}
    raw_by_id = {str(row["id"]): row for row in raw_rows}
    return {
        "url": str(url),
        "page": expected_page,
        "page_size": PAGE_SIZE,
        "total": total,
        "number_of_pages": expected_pages,
        "visible_ids": visible_ids,
        "rows": row_by_id,
        "raw_rows": raw_by_id,
        "detail_urls": {identity: links[identity] for identity in row_by_id if identity in links},
        "detail_link_labels": labels,
        "rsc_control_record_conflicts": [
            {"record_id": key, "previous": str(old)[:50], "replacement": str(new)[:50]}
            for key, old, new in conflicts
        ],
        "page_buttons": page_buttons,
        "unfiltered_state": {"keyword": "", "selectedLocations": ""},
    }


def _detail_rsc_row(html_bytes):
    records, _conflicts = decode_rsc_records(html_bytes)
    candidates = []
    for value in records.values():
        for item in _walk(value):
            if (isinstance(item, dict) and isinstance(item.get("data"), dict)
                    and "applyUrl" in item["data"] and "id" in item["data"]):
                candidates.append(resolve_rsc(item["data"], records))
    unique = {}
    for candidate in candidates:
        unique[str(candidate.get("id"))] = candidate
    if len(unique) != 1:
        raise ValueError(f"Gojek detail page has {len(unique)} native job records")
    return next(iter(unique.values()))


def parse_detail_page(job, html, url, row):
    """Bind a detail page to its exact listing row and retain every section."""
    parsed = urlsplit(str(url))
    parts = parsed.path.rstrip("/").split("/")
    if (parsed.scheme != "https" or parsed.hostname != "www.gojek.io"
            or len(parts) != 5 or parts[1:3] != ["careers", "view"]
            or parts[4] != job.job_id):
        raise ValueError("Gojek detail URL is not the exact native job route")

    html_bytes = html.encode("utf-8") if isinstance(html, str) else html
    soup = BeautifulSoup(html_bytes, "lxml")
    heading = soup.find("h1")
    rendered_title = normalized_text(heading.get_text(" ", strip=True) if heading else "")
    if rendered_title != normalized_text(row.get("text")):
        raise ValueError("Gojek exact detail title does not match its RSC listing row")
    location_node = soup.select_one("section.banner h5")
    visible_location = normalized_text(
        location_node.get_text(" ", strip=True) if location_node else ""
    )
    locations = [place for place in (row.get("categories") or {}).get("allLocations") or [] if place]
    if visible_location and locations and visible_location not in locations:
        raise ValueError("Gojek exact detail location differs from its RSC listing row")

    detail_row = _detail_rsc_row(html_bytes)
    for key in ("id", "text", "categories", "description", "lists", "additional",
                "hostedUrl", "applyUrl"):
        if detail_row.get(key) != row.get(key):
            raise ValueError(f"Gojek detail RSC field {key!r} differs from the listing row")

    column = soup.select_one('section[aria-label="Job details"] .description .col-lg-7')
    if column is None:
        raise ValueError("Gojek exact detail has no role-description column")
    visible = normalized_text(column.get_text(" ", strip=True))

    sections = role_sections(row)
    if not any(len(normalized_text(text_html(content))) >= 100 for _label, content in sections):
        raise ValueError("Gojek exact detail has no substantive role-description sections")
    generic = generic_company_sections(row)
    generic_text = generic["normalized_text"]
    if generic_text:
        if not visible.endswith(generic_text):
            raise ValueError(
                "Gojek generic company blocks are not the visible description suffix"
            )
        visible_role = normalized_text(visible[: -len(generic_text)])
    else:
        visible_role = visible

    matches, rendered, unrendered = [], [], []
    for label, content in sections:
        field_text = normalized_text(text_html(content))
        present = bool(field_text and field_text in visible_role)
        matches.append({
            "label": label,
            "visible": present,
            "normalized_chars": len(field_text),
        })
        (rendered if present else unrendered).append((label, content))
    if not rendered:
        raise ValueError("Gojek exact detail renders none of its role sections")

    description_html = build_description_html(rendered)
    stored_role = normalized_text(text_html(description_html))
    ratio = SequenceMatcher(None, visible_role, stored_role).ratio()
    if ratio < 0.9:
        raise ValueError(
            "Gojek stored role description does not match the rendered role body "
            f"(difflib {ratio:.3f})"
        )
    omissions = [
        {"label": label, "normalized_chars": len(normalized_text(text_html(content)))}
        for label, content in unrendered
    ]
    unrendered_sections = [
        {
            "label": label,
            "html": content,
            "normalized_chars": len(normalized_text(text_html(content))),
            "reason": "present_in_official_rsc_row_not_rendered_by_live_detail_page",
        }
        for label, content in unrendered
    ]

    apply_urls = []
    for anchor in soup.select("a.apply-job-btn[href]"):
        href = urljoin(str(url), anchor.get("href", ""))
        parsed_apply = urlsplit(href)
        if (parsed_apply.scheme != "https" or parsed_apply.hostname != "jobs.lever.co"
                or not parsed_apply.path.startswith(f"/GoToGroup/{job.job_id}/apply")):
            raise ValueError("Gojek Apply Now link is not for the exact listed job")
        apply_urls.append(href)
    if not apply_urls:
        raise ValueError("Gojek exact detail has no Apply Now link")
    row_apply = urlsplit(row.get("applyUrl") or "")
    if (row_apply.scheme != "https" or row_apply.hostname != "jobs.lever.co"
            or row_apply.path != f"/GoToGroup/{job.job_id}/apply"):
        raise ValueError("Gojek listing row has no exact same-job Lever apply URL")
    if any(urlsplit(value).path != row_apply.path for value in apply_urls):
        raise ValueError("Gojek Apply Now links disagree with the listing row")

    job.description_html = description_html
    job.description_method = (
        "gojek.official_careers.native_detail_role_sections_rendered_visible.2"
    )
    job.description_source_url = str(url)
    job.responsibilities = "\n\n".join(
        text_html(content) for label, content in rendered
        if any(token in label.casefold() for token in ROLE_LABELS_RESPONSIBILITY)
    ) or None
    job.qualifications = "\n\n".join(
        text_html(content) for label, content in rendered
        if any(token in label.casefold() for token in ROLE_LABELS_QUALIFICATION)
    ) or None
    categories = row.get("categories") or {}
    job.locations = [
        {"location": place, "country_code": row.get("country")} for place in locations
    ]
    job.department = categories.get("department")
    job.team = categories.get("team") if categories.get("team") not in (None, "", "-") else None
    job.employment_type = categories.get("commitment")
    job.workplace_type = row.get("workplaceType")
    job.apply_url = apply_urls[0]
    job.employer = None
    job.requisition_id = None
    html_tag = soup.find("html")
    job.language = html_tag.get("lang") if html_tag and html_tag.get("lang") else "en"
    job.date_posted = None
    job.date_updated = None
    job.valid_through = None
    job.salary = None
    job.raw["listing"] = row
    job.raw["createdAt"] = row.get("createdAt")
    job.raw["hostedUrl"] = row.get("hostedUrl")
    job.raw["applyUrl"] = row.get("applyUrl")
    job.raw["attachments"] = []
    job.raw["detail"] = {
        "id": job.job_id,
        "title": rendered_title,
        "url": str(url),
        "source_response_sha256": sha256(html_bytes).hexdigest(),
        "publisher_name": PUBLISHER,
        "role_sections": [
            {
                "label": label,
                "html": content,
                "rendered": normalized_text(text_html(content)) in visible_role,
            }
            for label, content in sections
        ],
        "role_section_matches_visible": matches,
        "unrendered_sections": unrendered_sections,
        "rendered_section_omissions": omissions,
        "generic_company_sections": generic,
        "visible_role_body": {
            "normalized_chars": len(visible_role),
            "fnv1a64": fnv1a64(visible_role),
            "sha256": sha256(visible_role.encode()).hexdigest(),
            "difflib_ratio_to_description": round(ratio, 6),
            "generic_suffix_normalized_chars": len(generic_text),
        },
        "description_policy": (
            "description_html/text contain only the role sections rendered by the live "
            "detail page; the shared GoTo corporate blocks are stored once under "
            "generic_company_sections and RSC-only sections under unrendered_sections."
        ),
        "apply_urls": apply_urls,
        "date_policy": (
            "createdAt is preserved only in raw source fields; no public posting date was observed."
        ),
        "salary_policy": "No structured salary field or published range was observed.",
    }
    if unrendered:
        job.warnings.append("some_role_sections_not_rendered_on_detail")
    return job


class Gojek(Adapter):
    VERIFICATION_REVISION = "gojek.official-careers-rsc-native-details.3"

    def __init__(self, http, company, source, options, capture=None):
        super().__init__(http, company, source, options)
        self.capture = capture or {}
        self.source_blocker = None
        self.detail_capture_used = 0
        self.http_detail_used = 0
        self.listing_capture_used = 0
        self.http_listing_used = 0

    def _single_attempt_http(self):
        if self.http is None:
            return None
        safe_http = copy(self.http)
        safe_http.retries = 0
        return safe_http

    def _mark_source_blocker(self, exc, url):
        match = re.search(r"\bHTTP\s+(403|405|429)\b", str(exc), re.I)
        if match:
            self.source_blocker = {
                "status": int(match.group(1)),
                "url": url,
                "error": str(exc),
                "retry": False,
            }
            self.result.metadata["source_blocker"] = self.source_blocker

    @staticmethod
    def _page_key(page):
        return str(page)

    def _capture_listing(self, page, final=False):
        group = "final_listing" if final else "initial_listing"
        value = (self.capture.get(group) or {}).get(self._page_key(page))
        if not value:
            return None
        return type(
            "CapturedPage",
            (),
            {
                "content": value["html"].encode("utf-8")
                if isinstance(value["html"], str) else value["html"],
                "url": value.get("url") or ENTRY_URL,
                "status": value.get("status"),
                "source": value.get("source", "browser_capture"),
            },
        )()

    async def _get_listing(self, page, final=False):
        captured = self._capture_listing(page, final)
        if captured is not None:
            if final:
                self.result.metadata.setdefault("final_listing_capture_used", 0)
                self.result.metadata["final_listing_capture_used"] += 1
            else:
                self.listing_capture_used += 1
            return captured
        url = ENTRY_URL if page == 1 else f"{ENTRY_URL}?{PAGE_QUERY}={page}"
        response = await self.http.get(url)
        if final:
            self.result.metadata.setdefault("final_http_listing_used", 0)
            self.result.metadata["final_http_listing_used"] += 1
        else:
            self.http_listing_used += 1
        return response

    async def listing_pass(self, final=False):
        total, rows, detail_urls, pages = None, None, {}, []
        complete = True
        for page in (1, 2):
            if not self.page_guard():
                complete = False
                break
            try:
                response = await self._get_listing(page, final=final)
                view = parse_listing_page(response.content, str(response.url), page)
            except FetchError as exc:
                self._mark_source_blocker(exc, str(getattr(exc, "url", ENTRY_URL)))
                self.result.errors.append(
                    f"gojek_native_listing_page_{page}_failed: {exc}"
                )
                complete = False
                break
            except (ValueError, KeyError, TypeError) as exc:
                self.result.errors.append(
                    f"gojek_native_listing_page_{page}_invalid: {type(exc).__name__}: {exc}"
                )
                complete = False
                break
            if total is None:
                total, rows = view["total"], view["rows"]
            elif total != view["total"] or rows != view["rows"]:
                self.result.errors.append(
                    "gojek_native_rsc_membership_or_fields_changed_between_page_views"
                )
                complete = False
                break
            pages.append({
                "page": page,
                "url": view["url"],
                "page_size": PAGE_SIZE,
                "total": view["total"],
                "number_of_pages": view["number_of_pages"],
                "visible_ids": view["visible_ids"],
                "embedded_ids": list(view["rows"]),
                "page_buttons": view["page_buttons"],
                "unfiltered_state": view["unfiltered_state"],
                "rsc_control_record_conflicts": view["rsc_control_record_conflicts"],
            })
            detail_urls.update(view["detail_urls"])
            progress = getattr(self.options, "source_progress", None)
            if progress:
                progress(self.result, page, total, view["visible_ids"])
        if rows is None:
            rows = {}
        if not self.source_blocker and complete:
            visible_ids = {identity for page in pages for identity in page["visible_ids"]}
            if visible_ids != set(rows):
                self.result.errors.append(
                    "gojek_native_paginated_cards_do_not_cover_rsc_membership"
                )
                complete = False
        key = "final_native_enumerations" if final else "native_enumerations"
        self.result.metadata.setdefault(key, []).append({
            "portal": ENTRY_URL,
            "total": total,
            "page_size": PAGE_SIZE,
            "pages": pages,
            "job_ids": list(rows),
            "complete": complete and total is not None and len(rows) == total,
            "capture_used": bool(self.listing_capture_used) if not final else
            bool(self.result.metadata.get("final_listing_capture_used")),
        })
        if not final and total is not None and self.result.expected_total is None:
            self.result.expected_total = total
        return rows, detail_urls, total, complete and total is not None and len(rows) == total

    def _detail_capture(self, job):
        value = (self.capture.get("details") or {}).get(str(job.job_id))
        if not value:
            return None
        return type(
            "CapturedDetail",
            (),
            {
                "content": value["html"].encode("utf-8")
                if isinstance(value["html"], str) else value["html"],
                "url": value.get("url") or job.url,
                "status": value.get("status"),
                "source": value.get("source", "browser_capture"),
            },
        )()

    async def detail(self, job):
        if self.source_blocker:
            raise FetchError(
                "Gojek detail stopped after a source block; no further request sent"
            )
        captured = self._detail_capture(job)
        if captured is not None:
            self.detail_capture_used += 1
            response = captured
        else:
            if self.capture.get("details"):
                raise FetchError("Gojek detail has no captured page for this exact job")
            self.http_detail_used += 1
            try:
                response = await self.http.get(job.url)
            except FetchError as exc:
                self._mark_source_blocker(exc, job.url)
                raise
        parsed, expected = urlsplit(str(response.url)), urlsplit(job.url)
        if (parsed.scheme != "https" or parsed.hostname != "www.gojek.io"
                or parsed.path != expected.path):
            raise ValueError("Gojek exact detail redirected from its native listed URL")
        soup = BeautifulSoup(response.content, "lxml")
        visible = normalized_text(soup.get_text(" ", strip=True))
        if re.search(r"human verification|verify you are human|captcha", visible, re.I):
            self._mark_source_blocker(
                ValueError("Gojek detail returned a human-verification challenge"),
                str(response.url),
            )
            raise ValueError("Gojek detail returned a human-verification challenge")
        return parse_detail_page(job, response.content, str(response.url), job.raw["listing"])

    async def run(self):
        original_http = self.http
        self.http = self._single_attempt_http()
        started = time.perf_counter()
        try:
            self.result.metadata.update(
                verification_revision=self.VERIFICATION_REVISION,
                official_entry=ENTRY_URL,
                official_listing_url=ENTRY_URL,
                native_pagination_query=PAGE_QUERY,
                native_page_size=PAGE_SIZE,
                listing_authority=(
                    "Gojek official careers RSC dataset; its rendered cards and page buttons "
                    "slice the full published openPositions array into 10-row UI pages."
                ),
                unfiltered_state={"keyword": "", "selectedLocations": ""},
                publisher_name=PUBLISHER,
                related_ecosystem_routes_not_enumerated=[GO_TO_GROUP_CAREERS],
                related_ecosystem_scope_note=(
                    "GoTo Financial and GoTo Group are separate business-unit/parent careers "
                    "routes; Gojek's own page states business units post on their respective "
                    "company websites. Only the Gojek list is counted here."
                ),
                source_mode=(
                    "headless_playwright_rendered_capture"
                    if self.capture else "http"
                ),
                http_429_note=(
                    "A plain HTTP request to the official site returned Vercel Security "
                    "Checkpoint HTTP 429 during this run; the rendered browser capture was "
                    "used without solving or forging a challenge."
                    if self.capture else None
                ),
            )
            first, first_urls, total, first_complete = await self.listing_pass()
            self.result.timings["native_listing_pass_seconds"] = (
                time.perf_counter() - started
            )
            if self.result.expected_total is not None and total != self.result.expected_total:
                self.result.errors.append("gojek_native_total_changed_between_passes")
                first_complete = False

            selected = list(first.items())
            sampled = bool(self.options.max_jobs and len(selected) > self.options.max_jobs)
            if sampled:
                selected = selected[:self.options.max_jobs]
                self.result.warnings.append("sample_limit")

            jobs = []
            for identity, row in selected:
                detail_url = first_urls.get(identity)
                if not detail_url:
                    self.result.errors.append(
                        f"gojek_native_detail_link_missing_{identity}"
                    )
                    first_complete = False
                    continue
                categories = row.get("categories") or {}
                raw = {
                    "listing": row,
                    "listing_url": ENTRY_URL,
                    "native_detail_url": detail_url,
                    "publisher_name": PUBLISHER,
                    "source_createdAt_preserved_not_as_post_date": row.get("createdAt"),
                    "source_hostedUrl": row.get("hostedUrl"),
                }
                jobs.append(self.job(
                    raw,
                    title=row.get("text"),
                    url=detail_url,
                    job_id=identity,
                    requisition_id=None,
                    employer=None,
                    locations=[
                        {"location": place, "country_code": row.get("country")}
                        for place in (
                            categories.get("allLocations") or [categories.get("location")]
                        ) if place
                    ],
                    department=categories.get("department"),
                    team=(
                        categories.get("team")
                        if categories.get("team") not in (None, "", "-") else None
                    ),
                    employment_type=categories.get("commitment"),
                    workplace_type=row.get("workplaceType"),
                ))

            await self.hydrate(jobs, self.detail)
            for job in jobs:
                self.add(job)

            if not self.source_blocker and len(selected) == len(first):
                final_ok = True
                final_started = time.perf_counter()
                try:
                    final_rows, _final_urls, final_total, final_ok = await self.listing_pass(
                        final=True
                    )
                    if (not final_ok or final_total != total
                            or list(final_rows) != list(first)
                            or final_rows != first):
                        raise ValueError(
                            "Gojek final listing identity/order/fields changed"
                        )
                    self.result.metadata["final_membership_reconciled"] = True
                except Exception as exc:
                    final_ok = False
                    self.result.errors.append(
                        f"gojek_final_membership_failed: {type(exc).__name__}: {exc}"
                    )
                    self.result.metadata["final_membership_reconciled"] = False
                self.result.timings["final_membership_seconds"] = (
                    max(0.0, time.perf_counter() - final_started)
                )
                first_complete = first_complete and final_ok

            self.result.listing_complete = bool(
                first_complete and total is not None and len(first) == total
            )
            self.result.metadata.update(
                collected_ids=list(first),
                detail_capture_requests_used=self.detail_capture_used,
                http_detail_requests_used=self.http_detail_used,
                listing_capture_requests_used=self.listing_capture_used,
                http_listing_requests_used=self.http_listing_used,
                detail_pages_requested=len(jobs),
                expected_total=total,
            )
            self.result.timings["source_collection_seconds"] = max(
                0.0, time.perf_counter() - started
            )
            self.result.finish()
            return self.result
        finally:
            self.http = original_http
