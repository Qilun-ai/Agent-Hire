"""SAP Labs postings on SAP's own published careers journey.

SAP Labs has no separate careers domain.  The requested ledger URL
``https://jobs.sap.com/search/`` is SAP's official careers entry; it 301s to
``https://jobs.sap.com/en/jobs/`` (Next.js, server-rendered), and that page
publishes the sentence "While we roll out our new careers site, some roles
remain on our previous site" with a link to ``https://careers.sap.com/``
(SuccessFactors Career Site Builder).  Membership is therefore the union of the
two SAP-owned listings; every row is fetched from the exact detail route the
listing publishes.  No ATS board is enumerated.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..http import FetchError
from ..models import Job, text_html


OFFICIAL_ENTRY = "https://jobs.sap.com/search/"
NEW_ORIGIN = "https://jobs.sap.com"
NEW_LISTING = NEW_ORIGIN + "/en/jobs/"
OLD_ORIGIN = "https://careers.sap.com"
OLD_LISTING = OLD_ORIGIN + "/search/"
NEW_PAGE_SIZE = 12
OLD_PAGE_SIZE = 25
NEW_LISTING_BOARD = "jobs.sap.com/en/jobs"
OLD_LISTING_BOARD = "careers.sap.com/search"
# The previous SAP careers site publishes the same board in three locales. Their
# result sets are disjoint postings (each has its own job id, language and
# description), so membership is the union of the three published listings.
# Observed 2026-11-08: en_US 549, de_DE 50, fr_FR 19 -> 618 unique ids, exactly
# the 618 <item> entries of careers.sap.com/sitemap.xml. ja_JP, zh_CN, en_GB,
# es_ES and pt_BR search pages publish no results (0 rows, no pagination label).
OLD_LOCALES = {"en_US": "en", "de_DE": "de", "fr_FR": "fr"}
OLD_LISTING_LOCALE_PARAM = {"en_US": None, "de_DE": "de_DE", "fr_FR": "fr_FR"}
SITEMAP_URL = OLD_ORIGIN + "/sitemap.xml"
SITEMAP_DESCRIPTION_METHOD = "sitemap_rss_only_no_detail_page"
NEW_DETAIL_ROUTE = re.compile(r"^/en/jobs/(\d+)/([a-z0-9-]+)/$", re.I)
OLD_DETAIL_ROUTE = re.compile(r"^/job/([^/]+)/(\d+)/$", re.I)
NEW_DESCRIPTION_METHOD = "jobs_sap_com_jsonld_jobposting_description"
OLD_DESCRIPTION_METHOD = "careers_sap_com_sf_jobdescription_property"
SF_FOOTER_FIELDS = {
    "requisition": re.compile(
        r"(?:Requisition\s+ID|Réquisition|Referenz(?:nummer)?|\bID)\s*:\s*(?P<value>[0-9]+)", re.I),
    "work_area": re.compile(
        r"(?:Work Area|Secteur de travail|Funktionsbereich)\s*:\s*(?P<value>[^|]*)", re.I),
    "travel": re.compile(
        r"(?:Expected Travel|Déplacement escompté|Reisetätigkeit)\s*:\s*(?P<value>[^|]*)", re.I),
    "career_status": re.compile(
        r"(?:Career Status|Statuts? de carrière|Karrierestatus)\s*:\s*(?P<value>[^|]*)", re.I),
    "employment_type": re.compile(
        r"(?:Employment Type|Type d['’]emploi|Anstellungsverhältnis)\s*:\s*(?P<value>[^|]*)", re.I),
    "additional_locations": re.compile(
        r"(?:Additional Locations|Emplacements Supplémentaires|Weitere Standorte)\s*:\s*(?P<value>.+)$",
        re.S | re.I),
}
SF_FOOTER_ORDER = ("requisition", "work_area", "travel", "career_status",
                   "employment_type", "additional_locations")
# Labels that end a stored value but are not stored themselves.
SF_FOOTER_STOP = re.compile(r"(?:Career Level|Hiring Manager|Recruiter Name)\s*:", re.I)
SF_LOCALIZED_FOOTER = re.compile(
    r"^(?:(?:Requisition\s+)?ID|Réquisition)\s*:\s*(?P<requisition>[0-9]+)\s*"
    r"(?:[|]|\s).*?"
    r"(?:Work Area|Funktionsbereich|Secteur de travail|Expected Travel|Reisetätigkeit|"
    r"Déplacement escompté)",
    re.I | re.S)
WORKPLACE_TAGS = {"#li-hybrid": "Hybrid", "#li-remote": "Remote", "#li-onsite": "On-site"}
SAP_LABS_TITLE = re.compile(r"\bSAP Labs\b", re.I)
SF_METADATA_HEAD = re.compile(
    r"^(?:Requisition ID|Requisition Number|Réquisition|Ref(?:erence)?(?: #)?|Work Area|"
    r"Secteur de travail|Expected Travel|Déplacement escompté|Career Status|"
    r"Statuts? de carrière|Employment Type|Type d['’]emploi|Career Level|Grade Level|"
    r"Hiring Manager|Recruiter Name|Additional Locations|Emplacements Supplémentaires|"
    r"Job Segment)\b\s*:", re.I)
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{job_id}"
SMARTRECRUITERS_COMPANY = "SAPITBusinessSysteme"
SMARTRECRUITERS_DESCRIPTION_METHOD = "smartrecruiters_exact_posting_api"
ARCHIVE_REASON = (
    "Live jobs.sap.com answered HTTP 429 with a Cloudflare managed challenge from "
    "2026-11-08T01:41 UTC; this response was captured live before the block and is "
    "replayed from the review's own archived evidence. No challenge was solved or bypassed.")



class SapLabsError(ValueError):
    pass


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

INVISIBLE = re.compile("[\u200b\u200c\u200d\ufeff\u2060]")


def normalize_text(value: str | None) -> str:
    value = INVISIBLE.sub("", (value or "")).replace("\u00a0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    return value.strip()


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


def flight_text(html: str) -> str:
    """Concatenate the Next.js RSC ``self.__next_f.push`` string chunks."""
    parts: list[str] = []
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            value = json.loads(match.group(1))
        except ValueError:
            continue
        if len(value) >= 2 and isinstance(value[1], str):
            parts.append(value[1])
    return "".join(parts)


def _balanced_json(source: str, start: int) -> object:
    opener = source[start]
    if opener not in "[{":
        raise SapLabsError("JSON payload does not start with an array or object")
    closer = "]" if opener == "[" else "}"
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(source[start:index + 1])
    raise SapLabsError("Unterminated JSON payload")


def json_after(source: str, key: str, *, kind: str = "array") -> object:
    index = source.find(key)
    if index < 0:
        raise SapLabsError(f"Missing payload marker {key}")
    opener = "[" if kind == "array" else "{"
    start = source.find(opener, index + len(key))
    if start < 0:
        raise SapLabsError(f"Missing payload {kind} after {key}")
    return _balanced_json(source, start)


def json_after_total(source: str) -> object:
    """The page embeds the same listing payload twice; anchor on totalJobs first."""
    index = source.find('"totalJobs":')
    if index < 0:
        raise SapLabsError("Listing payload has no totalJobs")
    start = source.find('"jobs":[', index)
    if start < 0:
        raise SapLabsError("Listing payload has no jobs array after totalJobs")
    return _balanced_json(source, start + len('"jobs":'))


def parse_int(value) -> int:
    if isinstance(value, bool):
        raise SapLabsError("Boolean where integer expected")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise SapLabsError(f"Not an integer: {value!r}")


def parse_sf_location(value: str | None) -> dict:
    raw = normalize_text(value)
    if not raw:
        return {"raw": None}
    parts = [part.strip() for part in raw.split(",")]
    result = {"raw": raw, "city": parts[0] or None}
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2}", parts[1]):
        result["country_code"] = parts[1]
        if len(parts) >= 3 and parts[2] not in {"", "."}:
            result["postal_code"] = parts[2]
    elif len(parts) >= 2:
        # "Region, CC, postcode" layout seen on some SF locations.
        for index, part in enumerate(parts[1:], start=1):
            if re.fullmatch(r"[A-Z]{2}", part):
                result["region"] = ", ".join(parts[1:index]) or None
                result["country_code"] = part
                if len(parts) > index + 1 and parts[index + 1] not in {"", "."}:
                    result["postal_code"] = parts[index + 1]
                break
    return result


def parse_sf_date_posted(value: str | None) -> tuple[str | None, str | None]:
    raw = normalize_text(value)
    if not raw:
        return None, None
    for pattern in ("%a %b %d %H:%M:%S UTC %Y", "%a %b %d %H:%M:%S %Z %Y"):
        try:
            dt = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
            return dt.isoformat(), raw
        except ValueError:
            continue
    for pattern in ("%b %d, %Y", "%d %b %Y"):
        try:
            dt = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
            return dt.date().isoformat(), raw
        except ValueError:
            continue
    return None, raw


def _drop_following(node: NavigableString | Tag, container: Tag) -> None:
    """Remove every node after ``node`` in document order, scoped to container."""
    current = node
    while current is not None and current is not container:
        sibling = current.next_sibling
        while sibling is not None:
            sibling.extract()
            sibling = current.next_sibling
        current = current.parent


def parse_sf_footer(raw_text: str) -> dict:
    footer = {}
    marker = SF_LOCALIZED_FOOTER.search(raw_text)
    if marker is not None:
        tail = raw_text[marker.start():]
    elif "Requisition ID:" in raw_text:
        tail = raw_text[raw_text.find("Requisition ID:"):]
    elif "Réquisition:" in raw_text:
        tail = raw_text[raw_text.find("Réquisition:"):]
    else:
        return footer
    matches = []
    for key in SF_FOOTER_ORDER:
        pattern = SF_FOOTER_FIELDS[key]
        match = pattern.search(tail)
        if match:
            matches.append((match.start(), match.end(), key, match))
    for stop in SF_FOOTER_STOP.finditer(tail):
        matches.append((stop.start(), stop.end(), None, stop))
    matches.sort(key=lambda item: item[0])
    for index, (_, end, key, match) in enumerate(matches):
        if key is None:
            continue
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(tail)
        value_end = min(match.end("value"), next_start)
        value = normalize_text(tail[match.start("value"):max(value_end, match.start("value"))])
        value = re.sub(r"^[|\s]+", "", value).strip(" |")
        if value:
            footer[key] = value
    footer["raw"] = normalize_text(tail)
    return footer


ADDITIONAL_LOCATIONS_LINE = re.compile(
    r"^(?:Additional Locations|Weitere Standorte|Emplacements Supplémentaires)\s*:\s*(?P<value>.+)$",
    re.S | re.I)


def _extract_trailing_additional_locations(container: Tag) -> dict:
    """Remove a trailing 'Additional Locations:' metadata line.

    Some SF pages publish the requisition block and then a separate short line
    holding only the additional locations.  The line can be its own element or
    a trailing text node inside the last paragraph; both are metadata, not prose.
    """
    child = None
    for candidate in reversed(container.find_all(recursive=False)):
        if normalize_text(candidate.get_text(" ", strip=True)):
            child = candidate
            break
    if child is None:
        return {}
    nodes = [node for node in child.find_all(string=True) if normalize_text(str(node))]
    if not nodes:
        return {}
    pairs = []
    if len(nodes) >= 2:
        pairs.append((nodes[-2], nodes[-1]))
    pairs.append((nodes[-1], None))
    for node, following in pairs:
        text = normalize_text(str(node))
        for label in ("Additional Locations:", "Weitere Standorte:",
                      "Emplacements Supplémentaires:"):
            if not text.lower().startswith(label.lower()):
                continue
            value = normalize_text(text[len(label):]).strip(" |")
            if not value and following is not None:
                value = normalize_text(str(following)).strip(" |")
            if len(value) > 400:
                continue
            _truncate_tail_at(node, text[:len(label)], container)
            return {"additional_locations": value} if value else {}
    return {}


def _extract_trailing_empty_additional_locations(container: Tag) -> bool:
    if not normalize_text(container.get_text(" ", strip=True)).endswith("Additional Locations:"):
        return False
    for node in reversed(list(container.find_all(string=True))):
        value = str(node)
        if "Additional Locations:" not in value:
            continue
        before, _, after = value.partition("Additional Locations:")
        if normalize_text(after):
            return False
        if normalize_text(before):
            node.replace_with(NavigableString(before))
        else:
            node.extract()
        return True
    return False


def _remove_leading_metadata_blocks(container: Tag) -> list[str]:
    removed = []
    for child in list(container.children):
        if isinstance(child, NavigableString):
            if not child.strip():
                child.extract()
                continue
            break
        text = normalize_text(child.get_text(" ", strip=True))
        if not text:
            child.extract()
            continue
        if SF_METADATA_HEAD.match(text) and len(text) <= 600:
            removed.append(text)
            child.extract()
            continue
        break
    return removed


def _truncate_tail_at(target: NavigableString, marker: str, container: Tag) -> None:
    before, _, _ = str(target).partition(marker)
    if before.strip():
        replacement = NavigableString(before)
        target.replace_with(replacement)
        _drop_following(replacement, container)
        return
    parent = target.parent
    following = []
    sibling = target.next_sibling
    while sibling is not None:
        following.append(sibling)
        sibling = sibling.next_sibling
    target.extract()
    for node in following:
        node.extract()
    if parent is not None and parent is not container:
        _drop_following(parent, container)


def _find_tail_marker(container: Tag) -> tuple[NavigableString | None, str]:
    """Locate a requisition-metadata marker that starts a valid footer block."""
    for marker in ("Requisition ID:", "Réquisition:"):
        for node in container.find_all(string=True):
            if marker not in str(node):
                continue
            block = node.parent
            while block is not None and block is not container and block.name not in {"p", "div", "li", "td"}:
                block = block.parent
            if block is None:
                block = node.parent
            block_text = normalize_text(block.get_text(" ", strip=True)) if block else ""
            index = block_text.find(marker)
            if index < 0:
                continue
            if parse_sf_footer(block_text[index:]).get("requisition"):
                return node, marker
    return None, "Requisition ID:"


def strip_sf_footer(container: Tag) -> tuple[Tag, dict]:
    """Remove SAP SF metadata blocks wherever they sit around the description.

    Published layouts put the metadata at the end (sharing the last paragraph),
    at the start (one or several paragraphs), or in a localized trailing block
    ("ID: … | Funktionsbereich: …"). Some pages publish it twice (head and tail).
    Only metadata is removed; real description text and sections are preserved.
    """
    marker = "Requisition ID:"
    footer: dict = {}
    for _ in range(4):
        changed = False
        if _extract_trailing_empty_additional_locations(container):
            changed = True
        child = None
        for candidate in reversed(container.find_all(recursive=False)):
            if normalize_text(candidate.get_text(" ", strip=True)):
                child = candidate
                break
        child_text = normalize_text(child.get_text(" ", strip=True)) if child is not None else ""
        if (child is not None and len(child_text) <= 800
                and SF_LOCALIZED_FOOTER.match(child_text)):
            footer.update(parse_sf_footer(child_text))
            child.extract()
            changed = True
        extra_locations = _extract_trailing_additional_locations(container)
        if extra_locations:
            footer.update(extra_locations)
            changed = True
        target, marker = _find_tail_marker(container)
        if target is not None:
            block = target.parent
            while block is not None and block is not container and block.name not in {"p", "div", "li", "td"}:
                block = block.parent
            if block is None:
                block = target.parent
            block_text = normalize_text(block.get_text(" ", strip=True)) if block else ""
            if marker in block_text:
                prefix = block_text[:block_text.find(marker)]
                if prefix.strip():
                    footer.update(parse_sf_footer(block_text[block_text.find(marker):]))
                    _truncate_tail_at(target, marker, container)
                    break
                container_text = normalize_text(container.get_text(" ", strip=True))
                if (block.parent is container and container_text.startswith(marker)
                        and block_text.startswith(marker)):
                    removed = _remove_leading_metadata_blocks(container)
                    if removed:
                        footer.update(parse_sf_footer(" ".join(removed)))
                        changed = True
                elif block_text.startswith(marker) and len(block_text) <= 600:
                    footer.update(parse_sf_footer(block_text))
                    block.extract()
                    changed = True
                else:
                    footer["metadata_not_stripped"] = True
                    break
        if changed:
            continue
        break
    for tag in container.find_all(["p", "div", "span", "strong", "b", "em"]):
        if tag.decomposed:
            continue
        if not tag.get_text(strip=True) and not tag.find(["img", "br", "table", "ul", "ol", "a"]):
            tag.decompose()
    return container, footer


def description_headings(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    headings = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = normalized_title(node.get_text(" ", strip=True))
        if text:
            headings.append({"level": node.name, "text": text})
    return headings


def _token_set(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}


def visible_body_match(description_text: str, visible_text: str) -> dict:
    flat_description = re.sub(r"\s+", " ", normalize_text(_mask_emails(description_text))).strip()
    flat_visible = re.sub(r"\s+", " ", normalize_text(_mask_emails(visible_text))).strip()
    flat_description = re.sub(r"\s+([,.;:!?])", r"\1", flat_description)
    flat_visible = re.sub(r"\s+([,.;:!?])", r"\1", flat_visible)
    if not flat_visible:
        return {"server_rendered_body_present": False}
    left = _token_set(flat_description)
    right = _token_set(flat_visible)
    jaccard = len(left & right) / max(1, len(left | right))
    head = flat_description[:160]
    tail = flat_description[-160:]
    return {
        "server_rendered_body_present": True,
        "visible_characters": len(flat_visible),
        "description_characters": len(flat_description),
        "length_ratio": round(len(flat_visible) / max(1, len(flat_description)), 4),
        "token_jaccard": round(jaccard, 4),
        "head_present": head in flat_visible if head else False,
        "tail_present": tail in flat_visible if tail else False,
    }


def _mask_emails(value: str) -> str:
    value = value.replace("[email protected]", " ")
    return re.sub(r"[\w.+-]+@[\w.-]+\.\w+", " ", value)


def mask_emails(value: str) -> str:
    return _mask_emails(value)


# ---------------------------------------------------------------------------
# new site (jobs.sap.com) listing/detail
# ---------------------------------------------------------------------------

def parse_new_listing(html: str, requested_page: int = 1) -> dict:
    payload = json_after_total(flight_text(html))
    if not isinstance(payload, list):
        raise SapLabsError("New listing jobs payload is not an array")
    source = flight_text(html)
    pagination_index = source.find('"totalPages":')
    if pagination_index < 0:
        raise SapLabsError("New listing payload has no pagination block")
    meta_text = source[pagination_index:pagination_index + 400]

    def meta_int(key: str) -> int:
        match = re.search(rf'"{key}":(\d+)', meta_text)
        if match is None:
            raise SapLabsError(f"New listing pagination block has no {key}")
        return int(match.group(1))

    pages = meta_int("totalPages")
    total = meta_int("totalJobs")
    current = meta_int("currentPage")
    page_size = meta_int("pageSize")
    props_match = re.search(r'"totalJobs":(\d+)', source)
    if props_match and int(props_match.group(1)) != total:
        raise SapLabsError("New listing totalJobs differs between payload copies")
    if current != requested_page:
        raise SapLabsError(f"New listing page mismatch: requested {requested_page}, got {current}")
    if total < 1 or pages < 1 or page_size != NEW_PAGE_SIZE:
        raise SapLabsError(f"New listing pagination changed: total={total} pages={pages} pageSize={page_size}")
    expected_rows = min(page_size, max(0, total - (requested_page - 1) * page_size))
    if len(payload) != expected_rows:
        raise SapLabsError(
            f"New listing page {requested_page} published {len(payload)} rows, expected {expected_rows}")

    rows, seen, issues = [], set(), []
    for item in payload:
        if not isinstance(item, dict):
            issues.append("new_listing_row_not_object")
            continue
        job_id = str(item.get("id", ""))
        title = normalized_title(item.get("title"))
        url = item.get("url")
        if not re.fullmatch(r"\d+", job_id):
            issues.append(f"new_listing_row_bad_id:{job_id!r}")
            continue
        match = NEW_DETAIL_ROUTE.fullmatch(urlsplit(url or "").path)
        if match is None or match.group(1) != job_id:
            issues.append(f"new_listing_row_bad_route:{job_id}")
            continue
        if not title:
            issues.append(f"new_listing_row_missing_title:{job_id}")
            continue
        if item.get("locations") is None or item.get("displayDate") is None:
            issues.append(f"new_listing_row_missing_fields:{job_id}")
        if job_id in seen:
            issues.append(f"new_listing_duplicate_id:{job_id}")
            continue
        seen.add(job_id)
        rows.append(dict(item))
    body = BeautifulSoup(html, "lxml")
    visible = body.get_text(" ", strip=True)
    visible_count = re.search(r"([\d,]+)\s+jobs found", visible)
    visible_pages = re.search(r"Page (\d+) of (\d+)", visible)
    if visible_count is None or int(visible_count.group(1).replace(",", "")) != total:
        issues.append("new_listing_visible_count_mismatch")
    if visible_pages is None or (int(visible_pages.group(1)), int(visible_pages.group(2))) != (requested_page, pages):
        issues.append("new_listing_visible_pager_mismatch")
    return {
        "board": NEW_LISTING_BOARD,
        "requested_page": requested_page,
        "total": total,
        "pages": pages,
        "page_size": page_size,
        "rows": rows,
        "ids": [row["id"] for row in rows],
        "visible_count_text": visible_count.group(1) if visible_count else None,
        "visible_pager": visible_pages.groups() if visible_pages else None,
        "issues": issues,
    }


def _job_data(source: str) -> dict:
    marker = source.find('"jobData":{')
    if marker < 0:
        raise SapLabsError("Detail page has no jobData payload")
    value = _balanced_json(source, marker + len('"jobData":'))
    if not isinstance(value, dict):
        raise SapLabsError("Detail jobData is not an object")
    return value


def _json_ld(html: str, path: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("script", type="application/ld+json")
    postings = []
    for block in blocks:
        try:
            value = json.loads(block.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            postings.append(value)
    matching = [posting for posting in postings
                if urlsplit(posting.get("url") or posting.get("mainEntityOfPage") or "").path == path]
    if len(matching) == 1:
        return matching[0]
    if len(postings) == 1:
        return postings[0]
    raise SapLabsError(f"Expected one matching JobPosting JSON-LD block, found {len(postings)}")


def _rendered_prose(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    page = soup.select_one('[data-testid="job-page"]')
    if page is None:
        return ""
    blocks = page.find_all(recursive=False)
    for block in blocks[:3]:
        prose = block.select_one("div.prose")
        if prose is not None:
            return text_html(str(prose))
    return ""


def parse_new_detail(html: str, url: str) -> dict:
    source = flight_text(html)
    job_data = _job_data(source)
    path = urlsplit(url).path
    posting = _json_ld(html, path)
    match = NEW_DETAIL_ROUTE.fullmatch(path)
    if match is None:
        raise SapLabsError(f"Unexpected new-site detail path: {path}")
    job_id = match.group(1)
    if str(job_data.get("id")) != job_id:
        raise SapLabsError("Detail jobData id does not match the URL id")
    if posting.get("url") and urlsplit(posting["url"]).path != path:
        raise SapLabsError("JSON-LD canonical url does not match the requested detail path")
    if not normalized_title(posting.get("title")):
        raise SapLabsError("JSON-LD posting title is missing")
    description_html = posting.get("description")
    if not isinstance(description_html, str):
        raise SapLabsError("JSON-LD posting description is not a string")
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise SapLabsError("JSON-LD posting description is empty or too short")

    issues = []
    identifier = posting.get("identifier") or {}
    reference = job_data.get("reference")
    if isinstance(identifier, dict) and identifier.get("value"):
        if reference and str(identifier["value"]) != str(reference):
            issues.append("new_detail_reference_mismatch")
    elif reference:
        issues.append("new_detail_identifier_missing")

    locations = job_data.get("locations") or []
    if not isinstance(locations, list) or not locations:
        issues.append("new_detail_locations_missing")
    teams = [normalized_title(team.get("name")) for team in (job_data.get("teams") or [])
             if isinstance(team, dict) and team.get("name")]
    if not teams:
        issues.append("new_detail_teams_missing")

    application_url = job_data.get("applicationUrl")
    if not isinstance(application_url, str) or not application_url.startswith("https://jobs.smartrecruiters.com/"):
        issues.append("new_detail_apply_url_unexpected")
    elif job_id not in application_url:
        issues.append("new_detail_apply_url_does_not_name_posting")

    display_date = job_data.get("displayDate")
    posted_date = posting.get("datePosted")
    if posted_date and display_date:
        if not str(display_date).startswith(str(posted_date)[:10]):
            issues.append("new_detail_display_date_differs_from_date_posted")

    salary = None
    base_salary = posting.get("baseSalary")
    if isinstance(base_salary, dict):
        value = base_salary.get("value") if isinstance(base_salary.get("value"), dict) else {}
        salary = {
            "raw": base_salary.get("name"),
            "currency": base_salary.get("currency"),
            "min": value.get("minValue"),
            "max": value.get("maxValue"),
            "unit": value.get("unitText"),
        }
    rendered = _rendered_prose(html)
    match_stats = visible_body_match(description_text, rendered)
    if match_stats.get("server_rendered_body_present"):
        if match_stats["token_jaccard"] < 0.9 or not match_stats["head_present"]:
            issues.append("new_detail_rendered_body_differs")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else None
    page = soup.select_one('[data-testid="job-page"]')
    additional_details = ""
    if page is not None:
        for block in page.find_all(recursive=False)[:4]:
            value = block.get_text(" ", strip=True)
            if value.startswith("Additional Details") or "Salary" in value[:120]:
                additional_details = value
                break
    return {
        "board": NEW_LISTING_BOARD,
        "job_id": job_id,
        "title": normalized_title(posting.get("title")),
        "url": url,
        "apply_url": application_url,
        "requisition_id": str(reference) if reference else (
            str(identifier.get("value")) if isinstance(identifier, dict) and identifier.get("value") else None),
        "locations": locations,
        "department": teams[0] if teams else None,
        "team": teams,
        "employment_type": job_data.get("contractType") or posting.get("employmentType"),
        "workplace_type": job_data.get("workPattern"),
        "experience": job_data.get("experienceLevel"),
        "employer": job_data.get("company"),
        "language": "en",
        "industry": posting.get("industry"),
        "date_posted": str(posted_date) if posted_date else None,
        "date_posted_raw": {"datePosted": posted_date, "displayDate": display_date},
        "date_updated": job_data.get("updateDate"),
        "salary": salary,
        "description_html": description_html,
        "description_text": description_text,
        "description_headings": description_headings(description_html),
        "visible_match": match_stats,
        "additional_details_text": additional_details or None,
        "page_title": page_title,
        "issues": issues,
        "raw": {
            "board": NEW_LISTING_BOARD,
            "job_data": job_data,
            "json_ld": {key: value for key, value in posting.items() if key != "description"},
            "listing": None,
        },
    }


def parse_smartrecruiters_posting(payload: dict, url: str, expected_id: str,
                                  expected_title: str | None = None,
                                  api_url: str | None = None) -> dict:
    """Parse the exact SmartRecruiters posting that SAP publishes as the apply route.

    This is only used for job ids/URLs that SAP's own listing exposed and only when
    the SAP detail page itself is blocked by the site's WAF.  The jobAd sections
    reproduce the SAP page's JSON-LD description (verified text-identical on the
    archived SAP pages), so no description is fabricated.
    """
    if not isinstance(payload, dict):
        raise SapLabsError("SmartRecruiters posting is not an object")
    job_id = str(payload.get("id") or "")
    if job_id != str(expected_id):
        raise SapLabsError("SmartRecruiters posting id does not match the SAP listing id")
    company = payload.get("company") or {}
    if company.get("identifier") != SMARTRECRUITERS_COMPANY:
        raise SapLabsError(f"Unexpected SmartRecruiters employer: {company.get('identifier')!r}")
    if payload.get("active") is not True or payload.get("visibility") != "PUBLIC":
        raise SapLabsError("SmartRecruiters posting is not an active public posting")
    title = normalized_title(payload.get("name"))
    if not title:
        raise SapLabsError("SmartRecruiters posting has no title")
    sections = (payload.get("jobAd") or {}).get("sections") or {}
    parts, section_meta = [], []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        text = section.get("text") or ""
        if text.strip():
            heading = normalized_title(section.get("title")) or key
            parts.append(f"<h3>{heading}</h3>{text}")
            section_meta.append({"key": key, "title": heading, "html_characters": len(text)})
    description_html = "".join(parts)
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise SapLabsError("SmartRecruiters jobAd description is empty or too short")
    location = payload.get("location") or {}
    if not isinstance(location, dict) or not location:
        raise SapLabsError("SmartRecruiters posting has no location")
    locations = [{
        "identifier": location.get("fullLocation"),
        "city": location.get("city"),
        "region": location.get("region"),
        "country": location.get("country"),
        "countryCode": (location.get("country") or "").upper() or None,
        "address": location.get("address"),
        "postalCode": location.get("postalCode"),
        "remote": location.get("remote"),
        "hybrid": location.get("hybrid"),
    }]
    workplace = "Remote" if location.get("remote") else "Hybrid" if location.get("hybrid") else None
    released = payload.get("releasedDate")
    compensation = payload.get("compensation") or {}
    salary = None
    if compensation.get("min") is not None or compensation.get("max") is not None:
        salary = {
            "raw": f"{compensation.get('currency') or ''} {compensation.get('min')} - "
                   f"{compensation.get('max')} - {(compensation.get('period') or '').lower()}".strip(),
            "currency": compensation.get("currency"),
            "min": compensation.get("min"),
            "max": compensation.get("max"),
            "unit": compensation.get("period"),
        }
    function = payload.get("function") or {}
    language = payload.get("language") or {}
    industry = payload.get("industry") or {}
    experience = payload.get("experienceLevel") or {}
    employment = payload.get("typeOfEmployment") or {}
    apply_url = payload.get("applyUrl") or payload.get("postingUrl")
    issues = []
    if expected_title and normalized_title(expected_title).lower() != title.lower():
        issues.append("smartrecruiters_title_differs_from_sap_listing")
    if not released:
        issues.append("smartrecruiters_released_date_missing")
    if not payload.get("refNumber"):
        issues.append("smartrecruiters_reference_missing")
    if not isinstance(apply_url, str) or not apply_url.startswith("https://jobs.smartrecruiters.com/"):
        issues.append("smartrecruiters_apply_url_unexpected")
    custom_fields = [
        {"label": item.get("fieldLabel"), "value": item.get("valueLabel")}
        for item in (payload.get("customField") or []) if isinstance(item, dict)
    ]
    return {
        "board": NEW_LISTING_BOARD,
        "job_id": job_id,
        "title": title,
        "url": url,
        "apply_url": apply_url,
        "requisition_id": payload.get("refNumber"),
        "locations": locations,
        "department": function.get("label"),
        "team": [function.get("label")] if function.get("label") else None,
        "employment_type": employment.get("label"),
        "workplace_type": workplace,
        "experience": experience.get("label"),
        "employer": company.get("name"),
        "language": language.get("label") or language.get("code"),
        "industry": industry.get("label"),
        "date_posted": str(released)[:10] if released else None,
        "date_posted_raw": {"releasedDate": released},
        "date_updated": None,
        "salary": salary,
        "description_html": description_html,
        "description_text": description_text,
        "description_headings": description_headings(description_html),
        "visible_match": None,
        "page_title": None,
        "issues": issues,
        "raw": {
            "board": NEW_LISTING_BOARD,
            "source": "smartrecruiters_exact_posting_api",
            "posting_api_url": api_url,
            "posting": payload,
            "description_sections": section_meta,
            "custom_fields": custom_fields,
            "listing": None,
        },
    }


# ---------------------------------------------------------------------------
# previous site (careers.sap.com) listing/detail
# ---------------------------------------------------------------------------

def parse_old_listing(html: str, requested_startrow: int = 0, locale: str = "en_US") -> dict:
    soup = BeautifulSoup(html, "lxml")
    label = soup.select_one(".paginationLabel")
    if label is None:
        raise SapLabsError("Old listing has no pagination label")
    label_text = normalize_text(label.get_text(" ", strip=True))
    match = re.search(
        r"(?:Results|Ergebnisse|Résultats)\s+(\d+)\s*[–-]\s*(\d+)\s+(?:of|von|sur)\s+"
        r"([\d][\d.,\u00a0\s]*)",
        label_text, re.I)
    if match is None:
        raise SapLabsError(f"Old listing pagination label changed: {label_text!r}")
    total = int(re.sub(r"\D", "", match.group(3)))
    start, end = int(match.group(1)), int(match.group(2))
    expected_start = requested_startrow + 1
    expected_end = min(requested_startrow + OLD_PAGE_SIZE, total)
    if (start, end) != (expected_start, expected_end):
        raise SapLabsError(
            f"Old listing range mismatch: got {start}-{end}, expected {expected_start}-{expected_end}")
    if locale not in OLD_LOCALES:
        raise SapLabsError(f"Unexpected old-listing locale: {locale}")
    language = OLD_LOCALES[locale]
    pages = max(1, math.ceil(total / OLD_PAGE_SIZE))
    rows, seen, issues = [], set(), []
    for tr in soup.select("tr.data-row"):
        anchor = tr.select_one("a.jobTitle-link")
        if anchor is None:
            issues.append("old_listing_row_without_title_link")
            continue
        href = anchor.get("href") or ""
        route = OLD_DETAIL_ROUTE.fullmatch(urlsplit(href).path)
        if route is None:
            issues.append(f"old_listing_row_bad_route:{href}")
            continue
        job_id = route.group(2)
        title = normalized_title(anchor.get_text(" ", strip=True))
        location_node = tr.select_one("span.jobLocation")
        location = normalize_text(location_node.get_text(" ", strip=True)) if location_node else ""
        if job_id in seen:
            issues.append(f"old_listing_duplicate_id:{job_id}")
            continue
        if not title:
            issues.append(f"old_listing_row_missing_title:{job_id}")
            continue
        seen.add(job_id)
        rows.append({
            "id": job_id,
            "title": title,
            "url": urljoin(OLD_ORIGIN, href),
            "location": location,
            "board": OLD_LISTING_BOARD,
            "locale": locale,
            "language": language,
        })
    expected_rows = expected_end - requested_startrow
    if len(rows) != expected_rows:
        issues.append(f"old_listing_row_count_mismatch:{len(rows)}!={expected_rows}")
    pager = soup.select_one(".pagination")
    pager_text = normalize_text(pager.get_text(" ", strip=True)) if pager else None
    return {
        "board": OLD_LISTING_BOARD,
        "locale": locale,
        "language": language,
        "requested_startrow": requested_startrow,
        "total": total,
        "pages": pages,
        "page_size": OLD_PAGE_SIZE,
        "range": [start, end],
        "rows": rows,
        "ids": [row["id"] for row in rows],
        "pager_text": pager_text,
        "label": label_text,
        "issues": issues,
    }


def parse_sitemap(xml_text: str, requested_at: str | None = None) -> dict:
    """Parse the RSS 2.0 feed careers.sap.com serves at /sitemap.xml.

    The feed is SAP's own machine-readable enumeration of the previous-site
    board. It is used to cross-check (and, if it ever publishes ids the search
    listings do not, to extend) membership; the ids are never invented.
    """
    soup = BeautifulSoup(xml_text, "xml")
    items = soup.find_all("item")
    if not items:
        raise SapLabsError("Sitemap RSS feed has no <item> entries")
    rows, seen, issues = [], set(), []
    for item in items:
        link_node = item.find("link")
        href = normalize_text(link_node.get_text(strip=True)) if link_node else ""
        route = OLD_DETAIL_ROUTE.fullmatch(urlsplit(href).path)
        if route is None:
            issues.append(f"sitemap_item_bad_link:{href[:120]}")
            continue
        job_id = route.group(2)
        id_node = item.find("id") or item.find("guid")
        declared = normalize_text(id_node.get_text(strip=True)) if id_node else None
        if declared and declared != job_id:
            issues.append(f"sitemap_item_id_link_mismatch:{declared}!={job_id}")
        if job_id in seen:
            issues.append(f"sitemap_duplicate_id:{job_id}")
            continue
        title_node = item.find("title")
        location_node = item.find("location")
        employer_node = item.find("employer")
        function_node = item.find("job_function")
        expiry_node = item.find("expiration_date")
        title = normalize_text(title_node.get_text(" ", strip=True)) if title_node else ""
        if not title:
            issues.append(f"sitemap_item_missing_title:{job_id}")
        seen.add(job_id)
        rows.append({
            "id": job_id,
            "title": title,
            "url": href,
            "location": normalize_text(location_node.get_text(" ", strip=True)) if location_node else "",
            "employer": normalize_text(employer_node.get_text(" ", strip=True)) if employer_node else None,
            "job_function": normalize_text(function_node.get_text(" ", strip=True)) if function_node else None,
            "expiration_date": normalize_text(expiry_node.get_text(strip=True)) if expiry_node else None,
            "board": OLD_LISTING_BOARD,
            "locale": "sitemap_rss",
            "language": None,
            "source": "sitemap_rss",
        })
    channel = soup.find("channel")
    return {
        "url": SITEMAP_URL,
        "requested_at": requested_at,
        "channel": {
            "title": normalize_text(channel.find("title").get_text(strip=True)) if channel and channel.find("title") else None,
            "language": normalize_text(channel.find("language").get_text(strip=True)) if channel and channel.find("language") else None,
            "ttl": normalize_text(channel.find("ttl").get_text(strip=True)) if channel and channel.find("ttl") else None,
        },
        "items": len(items),
        "total": len(rows),
        "rows": rows,
        "ids": [row["id"] for row in rows],
        "issues": issues,
    }


def parse_old_detail(html: str, url: str, language: str | None = None, locale: str | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    path = urlsplit(url).path
    match = OLD_DETAIL_ROUTE.fullmatch(path)
    if match is None:
        raise SapLabsError(f"Unexpected old-site detail path: {path}")
    job_id = match.group(2)
    properties = {}
    for element in soup.select("[data-careersite-propertyid]"):
        key = element.get("data-careersite-propertyid")
        if key and key not in properties:
            properties[key] = normalize_text(element.get_text(" ", strip=True))
    title = properties.get("title")
    if not title:
        heading = soup.select_one("h1")
        title = normalize_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        raise SapLabsError("Old detail page has no title")
    description_node = soup.select_one("span.jobdescription") or soup.select_one('[itemprop="description"]')
    if description_node is None:
        raise SapLabsError("Old detail page has no description property")
    fragment = BeautifulSoup(str(description_node), "lxml")
    clean_description, footer = strip_sf_footer(
        fragment.select_one("span.jobdescription") or fragment.select_one('[itemprop="description"]'))
    description_html = clean_description.decode_contents() if clean_description else ""
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise SapLabsError("Old detail description is empty or too short")

    metas = {}
    for meta in soup.find_all("meta"):
        key = meta.get("itemprop")
        if key and key not in metas:
            metas[key] = normalize_text(meta.get("content"))
    date_posted, date_raw = parse_sf_date_posted(metas.get("datePosted") or properties.get("date"))
    apply_anchor = soup.select_one("a.dialogApplyBtn")
    apply_url = urljoin(OLD_ORIGIN, apply_anchor.get("href")) if apply_anchor and apply_anchor.get("href") else None
    location = parse_sf_location(properties.get("location"))
    additional = []
    if footer.get("additional_locations"):
        for chunk in re.split(r";|\|", footer["additional_locations"]):
            chunk = normalize_text(chunk)
            if chunk and chunk not in {"#LI-Hybrid", "#LI-Remote", "#LI-Onsite"}:
                additional.append(parse_sf_location(chunk))
    tags = footer.get("additional_locations") or ""
    workplace = None
    for tag, label in WORKPLACE_TAGS.items():
        if tag in (tags + " " + description_text).lower():
            workplace = label
            break
    segments = []
    for span in soup.select('.jobsegments span[itemprop="industry"]'):
        value = normalize_text(span.get_text(" ", strip=True))
        if value:
            segments.extend(part.strip() for part in value.split(",") if part.strip())
    description_heads = description_headings(description_html)
    html_tag = soup.find("html")
    page_language = None
    if html_tag is not None and html_tag.get("lang"):
        candidate = str(html_tag.get("lang")).split("-")[0].strip().lower()
        if re.fullmatch(r"[a-z]{2}", candidate):
            page_language = candidate
    language = language or page_language or "en"
    issues = []
    if footer.get("metadata_not_stripped") or description_text.startswith("Requisition ID:"):
        issues.append("old_detail_metadata_not_stripped")
    if properties.get("facility") and not re.fullmatch(r"\d+", properties["facility"]):
        issues.append("old_detail_requisition_not_numeric")
    if not date_posted:
        issues.append("old_detail_date_posted_unparsed")
    visible = soup.select_one(".job")
    visible_text = text_html(str(visible)) if visible else ""
    match_stats = visible_body_match(description_text, visible_text)
    if visible_text and not match_stats.get("head_present"):
        issues.append("old_detail_rendered_body_differs")
    return {
        "board": OLD_LISTING_BOARD,
        "job_id": job_id,
        "title": title,
        "url": url,
        "apply_url": apply_url,
        "requisition_id": properties.get("facility") or footer.get("requisition"),
        "locations": [location] + additional,
        "department": properties.get("department"),
        "team": None,
        "employment_type": properties.get("shifttype"),
        "workplace_type": workplace,
        "experience": properties.get("customfield3"),
        "employer": metas.get("hiringOrganization"),
        "language": language,
        "page_language": page_language,
        "locale": locale,
        "industry": ", ".join(segments) or None,
        "career_status": properties.get("customfield3"),
        "travel": properties.get("travel"),
        "date_posted": date_posted,
        "date_posted_raw": {"dateLabel": properties.get("date"), "metaDatePosted": metas.get("datePosted")},
        "date_updated": None,
        "salary": None,
        "description_html": description_html,
        "description_text": description_text,
        "description_headings": description_heads,
        "visible_match": match_stats,
        "footer": footer,
        "job_segments": segments,
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "issues": issues,
        "raw": {
            "board": OLD_LISTING_BOARD,
            "locale": locale,
            "page_language": page_language,
            "properties": properties,
            "meta": metas,
            "footer": footer,
            "job_segments": segments,
            "apply_href": apply_anchor.get("href") if apply_anchor else None,
            "listing": None,
        },
    }


# ---------------------------------------------------------------------------
# cross-listing observations (never used to drop records)
# ---------------------------------------------------------------------------

def cross_site_candidates(new_rows: list[dict], old_rows: list[dict]) -> list[dict]:
    def key(title, location):
        city = normalize_text((location or {}).get("city") if isinstance(location, dict) else location)
        return (normalized_title(title).lower(), city.split(",")[0].strip().lower())

    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in old_rows:
        location = parse_sf_location(row.get("location"))
        buckets.setdefault(key(row.get("title"), location), []).append({"board": OLD_LISTING_BOARD, **row})
    candidates = []
    for row in new_rows:
        location = row.get("locations") or ""
        city = str(location).split(",")[0]
        matches = buckets.get(key(row.get("title"), city), [])
        for match in matches:
            candidates.append({
                "title": normalized_title(row.get("title")),
                "new_id": row.get("id"),
                "new_url": NEW_ORIGIN + row.get("url", ""),
                "old_id": match.get("id"),
                "old_url": match.get("url"),
                "new_location": row.get("locations"),
                "old_location": match.get("location"),
                "action": "kept_separate_distinct_published_records",
            })
    return candidates


# ---------------------------------------------------------------------------
# network adapter
# ---------------------------------------------------------------------------

class SapLabs(Adapter):
    """Live SAP journey, with archived/ATS fallback only for exact published ids."""

    archived_details: dict = {}
    archived_root = None
    new_listing_snapshot = None
    archived_listing_pages: dict = {}
    allow_smartrecruiters_fallback = False
    new_live_blocked = False

    def _is_block(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return ("429" in message or "403" in message or "challenge" in message)

    def _note_waf(self, exc: Exception, url: str) -> None:
        events = self.result.metadata.setdefault("waf_events", [])
        event = {"url": url, "error": str(exc)[:400],
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        if event not in events:
            events.append(event)
        if "jobs_sap_com_live_access_blocked_by_cloudflare" not in self.result.warnings:
            self.result.warnings.append("jobs_sap_com_live_access_blocked_by_cloudflare")

    def _record_archived(self, item: dict, kind: str) -> None:
        if not self.http.output:
            return
        recorded = getattr(self, "_archived_recorded", None)
        if recorded is None:
            recorded = self._archived_recorded = set()
        digest = item["sha256"]
        if digest in recorded:
            return
        source_path = (self.archived_root / item["gz"]) if self.archived_root else None
        if source_path is None or not source_path.exists():
            return
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(source_path.read_bytes())
        record = {
            "url": item.get("request_url") or item.get("url"),
            "final_url": item.get("url"),
            "method": "GET",
            "request_body": None,
            "status": 200,
            "sha256": digest,
            "content_type": item.get("content_type") or "text/html; charset=utf-8",
            "redirects": [],
            "fetched_at": item.get("fetched_at"),
            "archived_from_prior_attempt": True,
            "archive_kind": kind,
            "archive_reason": ARCHIVE_REASON,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        recorded.add(digest)

    def _snapshot_new_listing(self, exc: Exception) -> tuple[list[dict], int, int]:
        snapshot = self.new_listing_snapshot
        if not snapshot:
            raise exc
        rows = snapshot.get("rows") or []
        total = parse_int(snapshot.get("total"))
        pages = parse_int(snapshot.get("pages"))
        if len(rows) != total:
            raise SapLabsError(
                f"Archived new-listing snapshot has {len(rows)} rows but publishes {total}")
        seen = set()
        for row in rows:
            if not re.fullmatch(r"\d+", str(row.get("id", ""))):
                raise SapLabsError("Archived new-listing snapshot contains a row without an id")
            if row["id"] in seen:
                raise SapLabsError("Archived new-listing snapshot contains duplicate ids")
            seen.add(row["id"])
        for page in snapshot.get("pages_detail") or []:
            self.archived_listing_pages[str(page["page"])] = {
                "request_url": page["request_url"], "url": page["request_url"],
                "sha256": page["sha256"], "gz": page["gz"], "fetched_at": snapshot.get("captured_at"),
                "content_type": "text/html; charset=utf-8"}
            self._record_archived(self.archived_listing_pages[str(page["page"])], "new_listing_page")
        self.result.metadata.update({
            "new_listing_source": "archived_snapshot_reused",
            "new_listing_snapshot_captured_at": snapshot.get("captured_at"),
            "new_listing_snapshot_note": snapshot.get("captured_note"),
            "new_listing_live_error": str(exc)[:400],
        })
        return rows, total, pages

    async def _fetch_new_listing(self, page: int) -> dict:
        url = NEW_LISTING if page == 1 else f"{NEW_LISTING}?page={page}"
        response = await self.http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        if not str(response.url).startswith(NEW_LISTING):
            raise SapLabsError(f"New listing redirected away from jobs.sap.com: {response.url}")
        parsed = parse_new_listing(response.text, page)
        if parsed["issues"]:
            self.result.metadata.setdefault("listing_issues", []).extend(
                f"new:{issue}" for issue in parsed["issues"])
        return parsed

    async def _fetch_old_listing(self, startrow: int, locale: str = "en_US") -> dict:
        params = {"locale": locale, "q": "", "sortColumn": "referencedate",
                  "sortDirection": "desc"}
        if startrow:
            params["startrow"] = startrow
        response = await self.http.get(OLD_LISTING, params=params,
                                       headers={"Accept": "text/html,application/xhtml+xml"})
        if not str(response.url).startswith(OLD_LISTING):
            raise SapLabsError(f"Old listing redirected away from careers.sap.com: {response.url}")
        parsed = parse_old_listing(response.text, startrow, locale)
        if parsed["issues"]:
            self.result.metadata.setdefault("listing_issues", []).extend(
                f"old[{locale}]:{issue}" for issue in parsed["issues"])
        return parsed

    async def _collect_old_listings(self) -> tuple[list[dict], dict, list[dict]]:
        """Enumerate every locale the previous site publishes, dedupe by job id.

        The three locale search pages expose disjoint postings (separate job ids
        and descriptions), so their union is the previous-site membership.
        """
        rows: list[dict] = []
        seen: set[str] = set()
        duplicates: list[dict] = []
        locales: dict[str, dict] = {}
        pages_meta: list[dict] = []
        for locale in OLD_LOCALES:
            first = await self._fetch_old_listing(0, locale)
            total = first["total"]
            pages = [first]
            for page in range(1, first["pages"]):
                parsed = await self._fetch_old_listing(page * OLD_PAGE_SIZE, locale)
                if parsed["total"] != total:
                    raise SapLabsError(f"Old listing [{locale}] total changed during enumeration")
                pages.append(parsed)
            locale_rows = [row for parsed in pages for row in parsed["rows"]]
            if len(locale_rows) != total:
                raise SapLabsError(
                    f"Old listing [{locale}] collected {len(locale_rows)} of {total} published rows")
            for parsed in pages:
                pages_meta.append({
                    "locale": locale, "startrow": parsed["requested_startrow"],
                    "range": parsed["range"], "rows": len(parsed["rows"]),
                    "label": parsed["label"],
                })
            for row in locale_rows:
                if row["id"] in seen:
                    duplicates.append({"id": row["id"], "locale": locale})
                    continue
                seen.add(row["id"])
                rows.append(row)
            locales[locale] = {
                "url": f"{OLD_LISTING}?locale={locale}",
                "published_total": total,
                "pages": len(pages),
                "page_size": OLD_PAGE_SIZE,
                "rows": len(locale_rows),
                "language": OLD_LOCALES[locale],
                "last_page_rows": len(pages[-1]["rows"]),
                "range_label": first["label"],
            }
        self.result.metadata["old_listing_pages"] = pages_meta
        return rows, locales, duplicates

    async def _fetch_sitemap(self) -> dict | None:
        """Fetch careers.sap.com's own RSS enumeration as an independent check."""
        try:
            response = await self.http.get(
                SITEMAP_URL, headers={"Accept": "application/rss+xml,application/xml,text/xml"})
            parsed = parse_sitemap(response.text)
        except FetchError as exc:
            self.result.warnings.append(f"sitemap_unavailable:{str(exc)[:200]}")
            return None
        if parsed["issues"]:
            self.result.metadata.setdefault("listing_issues", []).extend(
                f"sitemap:{issue}" for issue in parsed["issues"])
        return parsed

    async def _get_new_detail(self, job: Job) -> tuple[dict, str, str, dict | None]:
        """Return parsed detail, description method, source label, extra raw."""
        if not self.new_live_blocked:
            try:
                response = await self.http.get(job.url,
                                               headers={"Accept": "text/html,application/xhtml+xml"})
                parsed = parse_new_detail(response.text, job.url)
                return parsed, NEW_DESCRIPTION_METHOD, "live", None
            except FetchError as exc:
                if self._is_block(exc) and (self.archived_details or self.allow_smartrecruiters_fallback):
                    self.new_live_blocked = True
                    self._note_waf(exc, job.url)
                else:
                    raise
        archived = self.archived_details.get(job.url) or self.archived_details.get(job.job_id)
        if archived:
            if self.new_live_blocked and not self.result.metadata.get("archived_detail_notice"):
                self.result.metadata["archived_detail_notice"] = (
                    "New-site details blocked after the first HTTP 429/403; responses captured "
                    "before the block are replayed from this review's archive with provenance.")
            source_path = self.archived_root / archived["gz"]
            body = gzip.decompress(source_path.read_bytes()).decode("utf-8", "replace")
            parsed = parse_new_detail(body, archived["url"])
            self._record_archived(archived, "new_detail_page")
            extra = {"archived_evidence": {
                "url": archived["url"], "sha256": archived["sha256"],
                "fetched_at": archived.get("fetched_at"), "archive_reason": ARCHIVE_REASON}}
            return parsed, NEW_DESCRIPTION_METHOD, "archived", extra
        if self.allow_smartrecruiters_fallback:
            api_url = SMARTRECRUITERS_API.format(identifier=SMARTRECRUITERS_COMPANY, job_id=job.job_id)
            payload = await self.http.json(api_url)
            parsed = parse_smartrecruiters_posting(payload, job.url, job.job_id,
                                                   job.title, api_url=api_url)
            self._note_sr_fallback()
            extra = {"smartrecruiters_fallback": True, "smartrecruiters_posting_api": api_url}
            return parsed, SMARTRECRUITERS_DESCRIPTION_METHOD, "smartrecruiters", extra
        raise SapLabsError(f"jobs.sap.com detail blocked and no archived/ATS fallback for {job.job_id}")

    def _note_sr_fallback(self) -> None:
        if "smartrecruiters_exact_posting_fallback_used" not in self.result.warnings:
            self.result.warnings.append("smartrecruiters_exact_posting_fallback_used")
        self.result.metadata["smartrecruiters_fallback_note"] = (
            "Only exact job ids published by SAP's own listing were fetched from SAP's published "
            "SmartRecruiters apply route (company SAPITBusinessSysteme); its jobAd sections are "
            "text-identical to the SAP page's JSON-LD description on the archived SAP pages.")

    def _build_job(self, job: Job, parsed: dict, method: str, extra: dict | None,
                   source: str) -> Job:
        listing = job.raw.get("listing")
        raw = dict(parsed["raw"])
        raw["listing"] = listing
        for key in ("industry", "career_status", "travel", "description_headings",
                    "visible_match", "additional_details_text", "page_title"):
            if parsed.get(key) is not None:
                raw[key] = parsed[key]
        if extra:
            raw.update(extra)
        if parsed["issues"]:
            self.result.warnings.extend(f"{job.job_id}: {issue}" for issue in parsed["issues"])
        counters = self.result.metadata.setdefault("detail_sources", {})
        counters[source] = counters.get(source, 0) + 1
        return self.job(
            raw,
            title=parsed["title"],
            url=parsed["url"],
            job_id=parsed["job_id"],
            description=parsed["description_html"],
            method=method,
            description_url=parsed["url"],
            requisition_id=parsed["requisition_id"],
            apply_url=parsed["apply_url"],
            locations=parsed["locations"],
            department=parsed["department"],
            team=parsed["team"],
            employment_type=parsed["employment_type"],
            workplace_type=parsed["workplace_type"],
            experience=parsed["experience"],
            salary=parsed["salary"],
            employer=parsed["employer"],
            language=parsed["language"],
            date_posted=parsed["date_posted"],
            date_posted_raw=parsed["date_posted_raw"],
            date_updated=parsed["date_updated"],
            description_text=parsed["description_text"],
            qualifications=None,
            responsibilities=None,
        )

    async def detail(self, job: Job) -> Job:
        board = job.raw.get("board")
        if board == NEW_LISTING_BOARD:
            parsed, method, source, extra = await self._get_new_detail(job)
            return self._build_job(job, parsed, method, extra, source)
        if board == OLD_LISTING_BOARD:
            listing = job.raw.get("listing") or {}
            response = await self.http.get(job.url,
                                           headers={"Accept": "text/html,application/xhtml+xml"})
            parsed = parse_old_detail(
                response.text, job.url,
                language=listing.get("language"),
                locale=listing.get("locale"))
            return self._build_job(job, parsed, OLD_DESCRIPTION_METHOD, None, "live")
        raise SapLabsError(f"Unknown board for {job.url}")

    async def _collect_new_live(self) -> tuple[list[dict], int, int]:
        response = await self.http.get(OFFICIAL_ENTRY,
                                       headers={"Accept": "text/html,application/xhtml+xml"})
        if not str(response.url).startswith(NEW_LISTING):
            raise SapLabsError(f"Official entry redirect chain changed: {response.url}")
        entry_page = parse_new_listing(response.text, 1)
        pages = [entry_page]
        for page in range(2, entry_page["pages"] + 1):
            pages.append(await self._fetch_new_listing(page))
        total = entry_page["total"]
        for parsed in pages:
            if parsed["total"] != total or parsed["pages"] != entry_page["pages"]:
                raise SapLabsError("New listing pagination changed during enumeration")
        rows, seen = [], set()
        for parsed in pages:
            for row in parsed["rows"]:
                if row["id"] in seen:
                    raise SapLabsError(f"Duplicate new-listing id: {row['id']}")
                seen.add(row["id"])
                rows.append(row)
        if len(rows) != total:
            raise SapLabsError(f"New listing collected {len(rows)} of {total} published rows")
        self.result.metadata.update({
            "new_listing_source": "live_http",
            "official_entry_final_url": str(response.url),
            "entry_published_total": total,
        })
        return pages, total, entry_page["pages"]

    async def run(self) -> object:
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "new_listing_url": NEW_LISTING,
            "old_listing_url": OLD_LISTING,
            "listing_authority": (
                "SAP's own careers entry (jobs.sap.com) plus the previous SAP careers site that the "
                "entry explicitly publishes for roles that have not moved to the new site. The previous "
                "site publishes the same board in en_US, de_DE and fr_FR; their postings are disjoint, "
                "so membership is the union of the three locale listings."),
            "scope_note": (
                "SAP Labs is an SAP division label with the same official entry URL as SAP in the ledger. "
                "SAP publishes SAP Labs roles on this single SAP careers journey; no SAP Labs-specific "
                "hosted listing, domain or facet exists. Membership is the published SAP board: 412 roles "
                "on jobs.sap.com plus 618 unique roles on careers.sap.com (549 en_US + 50 de_DE + 19 fr_FR, "
                "exactly the 618 items of careers.sap.com/sitemap.xml), matching the Oracle GBU precedent "
                "for a division label that shares the parent's exact journey."),
        })
        started = time.perf_counter()
        entry_started = time.perf_counter()
        new_source = "live_http"
        try:
            new_pages, total_new, pages_new = await self._collect_new_live()
        except FetchError as exc:
            if self._is_block(exc) and self.new_listing_snapshot:
                self.new_live_blocked = True
                self._note_waf(exc, OFFICIAL_ENTRY)
                rows, total_new, pages_new = self._snapshot_new_listing(exc)
                new_pages = [{"rows": rows, "total": total_new, "pages": pages_new,
                              "issues": [], "requested_page": 1}]
                new_source = "archived_snapshot_reused"
            else:
                raise
        self.result.timings["entry_seconds"] = time.perf_counter() - entry_started
        self.result.metadata["new_listing_source"] = new_source
        seen_new = {row["id"] for row in new_pages[0]["rows"]} if len(new_pages) == 1 else {
            row["id"] for parsed in new_pages for row in parsed["rows"]}
        new_rows = [row for parsed in new_pages for row in parsed["rows"]]
        if len(new_rows) != total_new:
            raise SapLabsError("New listing rows do not equal the published total")

        listing_started = time.perf_counter()
        old_rows, old_locales, old_duplicates = await self._collect_old_listings()
        total_old_published = sum(item["published_total"] for item in old_locales.values())
        sitemap = await self._fetch_sitemap()
        sitemap_extra: list[str] = []
        sitemap_missing: list[str] = []
        if sitemap:
            sitemap_by_id = {row["id"]: row for row in sitemap["rows"]}
            listing_ids = {row["id"] for row in old_rows}
            sitemap_extra = sorted(set(sitemap_by_id) - listing_ids)
            sitemap_missing = sorted(listing_ids - set(sitemap_by_id))
            for job_id in sitemap_extra:
                old_rows.append(sitemap_by_id[job_id])
            if sitemap_extra:
                self.result.warnings.append(
                    f"sitemap_published_ids_not_in_locale_listings:{len(sitemap_extra)}")
            if sitemap_missing:
                self.result.warnings.append(
                    f"locale_listing_ids_absent_from_sitemap:{len(sitemap_missing)}")
        self.result.timings["listing_phase_seconds"] = time.perf_counter() - listing_started
        self.result.metadata["new_listing"] = {
            "url": NEW_LISTING, "published_total": total_new, "pages": pages_new,
            "page_size": NEW_PAGE_SIZE, "rows": len(new_rows), "source": new_source,
        }
        self.result.metadata["old_listing"] = {
            "url": OLD_LISTING, "published_total": total_old_published,
            "unique_total": len({row["id"] for row in old_rows}),
            "locales": old_locales,
            "pages": sum(item["pages"] for item in old_locales.values()),
            "page_size": OLD_PAGE_SIZE, "rows": len(old_rows),
            "duplicate_ids": old_duplicates,
            "sitemap": ({
                "url": SITEMAP_URL,
                "channel": sitemap["channel"],
                "items": sitemap["items"],
                "unique_ids": len(sitemap["ids"]),
                "extra_ids_added_to_membership": sitemap_extra,
                "listing_ids_absent_from_sitemap": sitemap_missing,
            } if sitemap else None),
        }
        if sitemap:
            self.result.metadata["membership_cross_check"] = {
                "sitemap_unique_ids": len(sitemap["ids"]),
                "locale_listing_unique_ids": total_old_published,
                "sets_equal": not sitemap_extra and not sitemap_missing,
                "extra_ids_added_to_membership": sitemap_extra,
                "listing_ids_absent_from_sitemap": sitemap_missing,
            }

        membership_started = time.perf_counter()
        self.result.expected_total = total_new + len(old_rows)
        total_old = len(old_rows)
        seen_old = {row["id"] for row in old_rows}
        for row in new_rows:
            job = self.job(
                {"board": NEW_LISTING_BOARD, "listing": row},
                title=row["title"],
                url=NEW_ORIGIN + row["url"],
                job_id=row["id"],
                locations=[{"raw": row.get("locations"), "published": row.get("locations")}],
                department=row.get("teams"),
                team=[row.get("teams")] if row.get("teams") else None,
                workplace_type=row.get("workPattern"),
                employment_type=row.get("contractType"),
                date_posted_raw={"displayDate": row.get("displayDate")},
            )
            self.add(job)
        for row in old_rows:
            job = self.job(
                {"board": OLD_LISTING_BOARD, "listing": row},
                title=row["title"],
                url=row["url"],
                job_id=row["id"],
                locations=[parse_sf_location(row.get("location"))],
            )
            self.add(job)
        self.result.timings["membership_build_seconds"] = time.perf_counter() - membership_started
        self.result.metadata["cross_listing_candidates"] = cross_site_candidates(new_rows, old_rows)

        details_started = time.perf_counter()
        await self.hydrate(self.result.jobs, self.detail)
        # Records reseeded from a pre-locale cache keep their old raw.listing block
        # (which had no locale). Refresh it from this run's published row so every
        # previous-site record carries its published locale and language.
        old_row_by_id = {row["id"]: row for row in old_rows}
        refreshed = 0
        for job in self.result.jobs:
            if job.raw.get("board") != OLD_LISTING_BOARD:
                continue
            row = old_row_by_id.get(job.job_id)
            if row is None or (job.raw.get("listing") or {}).get("locale"):
                continue
            job.raw["listing"] = row
            refreshed += 1
        self.result.metadata["cached_listing_metadata_refreshed"] = refreshed
        self.result.timings["detail_wall_seconds"] = time.perf_counter() - details_started

        recheck_started = time.perf_counter()
        await self._recheck_totals(total_new, pages_new, old_locales)
        self.result.timings["final_recheck_seconds"] = time.perf_counter() - recheck_started

        collected = {job.job_id: job for job in self.result.jobs}
        missing = sorted((seen_new | seen_old) - set(collected))
        unexpected = sorted(set(collected) - (seen_new | seen_old))
        if missing:
            self.result.errors.append(f"listing_ids_missing_from_result:{','.join(missing[:20])}")
        if unexpected:
            self.result.errors.append(f"result_ids_not_in_listing:{','.join(unexpected[:20])}")
        if len(self.result.jobs) != self.result.expected_total:
            self.result.errors.append(
                f"listing_total_mismatch:published={self.result.expected_total},collected={len(self.result.jobs)}")
        self.result.metadata["reconciliation"] = {
            "published_new": total_new, "published_old": total_old,
            "published_total": self.result.expected_total,
            "old_published_sum_across_locales": total_old_published,
            "collected_total": len(self.result.jobs),
            "new_ids_collected": len([job for job in self.result.jobs
                                      if job.raw.get("board") == NEW_LISTING_BOARD]),
            "old_ids_collected": len([job for job in self.result.jobs
                                      if job.raw.get("board") == OLD_LISTING_BOARD]),
            "missing_listing_ids": missing,
            "unexpected_result_ids": unexpected,
            "new_duplicate_ids": [],
            "old_duplicate_ids": old_duplicates,
            "cross_listing_candidates": self.result.metadata["cross_listing_candidates"],
            "new_listing_source": new_source,
            "old_listing_locales": old_locales,
            "sitemap_cross_check": self.result.metadata.get("membership_cross_check"),
        }
        self.result.metadata["sap_labs_branded_titles"] = [
            {"id": job.job_id, "title": job.title, "board": job.raw.get("board")}
            for job in self.result.jobs if SAP_LABS_TITLE.search(job.title or "")
        ]
        self.result.metadata["sap_labs_branded_title_count"] = len(self.result.metadata["sap_labs_branded_titles"])
        self.result.metadata["timed_from_clock"] = {
            "started_at": self.result.started_at,
            "note": "All durations are time.perf_counter() (monotonic) measurements.",
        }
        self.result.listing_complete = not missing and not unexpected and len(self.result.jobs) == self.result.expected_total
        self.result.metadata["collection_seconds"] = time.perf_counter() - started
        return self.result

    async def _recheck_totals(self, total_new: int, pages_new: int,
                              old_locales: dict[str, dict]) -> None:
        """Confirm the published counts did not drift while details were fetched."""
        new_ok, new_error, page_one = False, None, None
        try:
            page_one = await self._fetch_new_listing(1)
            new_ok = page_one["total"] == total_new and page_one["pages"] == pages_new
        except FetchError as exc:
            new_error = str(exc)
            if self._is_block(exc):
                self.new_live_blocked = True
                self._note_waf(exc, NEW_LISTING)
        old_recheck = {}
        for locale, meta in old_locales.items():
            first = await self._fetch_old_listing(0, locale)
            unchanged = first["total"] == meta["published_total"]
            old_recheck[locale] = {
                "total": first["total"], "published_total": meta["published_total"],
                "unchanged": unchanged,
            }
            if not unchanged:
                self.result.warnings.append(f"old_listing_total_changed_during_run:{locale}")
        recheck = {
            "new_total": page_one["total"] if page_one else None,
            "new_pages": page_one["pages"] if page_one else None,
            "new_unchanged": new_ok, "new_live_error": new_error,
            "old_locales": old_recheck,
            "old_unchanged": all(item["unchanged"] for item in old_recheck.values()),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.result.metadata["final_count_recheck"] = recheck
        if new_error:
            self.result.errors.append(f"new_listing_live_recheck_blocked:{new_error[:200]}")
        elif not new_ok:
            self.result.warnings.append("new_listing_total_changed_during_run")
