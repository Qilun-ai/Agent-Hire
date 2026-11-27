"""Harness's own careers listing and its company-scoped Greenhouse board.

The inspected Harness careers journey is:

  https://www.harness.io/company/careers  (official entry)
      -> "View Open Positions" -> https://www.harness.io/company/jobs
      -> inline Webflow script fetches boards-api.greenhouse.io board "harnessinc"
      -> cards link to https://www.harness.io/company/jobs/apply?gh_jid=<id>
      -> that page embeds job-boards.greenhouse.io/embed/job_app?for=harnessinc&token=<id>
         whose .job__description is the full employer description.

Only the harnessinc board requested by Harness's own listing page is read.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import unescape
import re
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.harness.io/company/careers"
LISTING_URL = "https://www.harness.io/company/jobs"
BOARD_TOKEN = "harnessinc"
BOARD_ORIGIN = "https://boards-api.greenhouse.io"
JOBS_API = f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/jobs"
DEPARTMENTS_API = f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/departments"
OFFICES_API = f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/offices"
APPLY_URL = "https://www.harness.io/company/jobs/apply"
DETAIL_EMBED = "https://job-boards.greenhouse.io/embed/job_app"

BOARD_URL_RE = re.compile(
    r"https://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)/(jobs|departments|offices)(?:\?([^\"'\s<]*))?"
)
JOB_LINK_TEMPLATE = re.compile(r"/company/jobs/apply\?gh_jid=\$\{[^}]+\}")

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "ul", "ol", "table", "blockquote", "pre", "hr", "figure"}
CONTAINER_TAGS = {"div", "section", "article", "main", "header", "footer"}
RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|what you(?:'|’)?ll do|what you will do|what you(?:'|’)?ll be doing|"
    r"key duties|the role|your role|day[- ]to[- ]day",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualif|requirement|what you(?:'|’)?ll need|what you will need|what you bring|"
    r"what you(?:'|’)?ll bring|who you are|must have|nice to have|preferred|required|"
    r"experience|skills|about you|what we(?:'|’)?re looking for|technical (?:strength|expertise)",
    re.I,
)
PAY_TEXT = re.compile(
    r"Pay transparency\s*:?\s*(\$[\d,]+(?:\s*[—–-]\s*\$[\d,]+)?(?:\s*[A-Z]{3})?)",
    re.I,
)


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalize_for_compare(value: str | None) -> str:
    """The rendered page uses a hyphen where the API text has an em dash.

    Collapse whitespace and normalise typographic minus/dash glyphs only for
    nickname-insensitive body comparison. Stored source text is unchanged.
    """
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
    return re.sub(r"\s+", " ", value).strip()


def decode_html_wrapper(value: str | None) -> str:
    """Decode the whole escaped HTML wrapper returned by this board API.

    Only decode when the value *entirely* looks like escaped HTML, so literal
    code entities such as ``&lt;T&gt;`` inside a normal description are kept.
    """
    value = value or ""
    start = r"^\s*&(?:amp;)*lt;(?:p|div|section|article|ul|ol|h[1-6])(?:\s|&(?:amp;)*gt;)"
    end = r"&(?:amp;)*lt;/(?:p|div|section|article|ul|ol|h[1-6])&(?:amp;)*gt;\s*$"
    for _ in range(2):
        if re.search(start, value, re.I) and re.search(end, value, re.I):
            value = unescape(value)
        else:
            break
    return value


def _parse_iso(value, label: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Harness {label} is not a string: {value!r}")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Harness {label} is not an ISO timestamp: {value!r}") from exc
    return value


def _canonical_apply_url(job_id: str, absolute_url: str | None) -> str:
    expected = f"{APPLY_URL}?gh_jid={job_id}"
    if not absolute_url:
        return expected
    parts = urlsplit(absolute_url)
    if parts.scheme != "https" or parts.netloc != "www.harness.io" or parts.path != "/company/jobs/apply":
        raise ValueError(f"Harness job absolute_url is not its own apply route: {absolute_url!r}")
    job_ids = [value for key, value in parse_qsl(parts.query, keep_blank_values=True) if key == "gh_jid"]
    if not job_ids or any(value != job_id for value in job_ids):
        raise ValueError(f"Harness job absolute_url does not identify the same posting: {absolute_url!r}")
    return expected


def detail_embed_url(job_id: str) -> str:
    return DETAIL_EMBED + "?" + urlencode({"for": BOARD_TOKEN, "token": job_id})


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.harness.io" or parts.path.rstrip("/") != "/company/careers":
        raise ValueError("Harness official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at Harness" not in title:
        raise ValueError("Harness official careers entry title changed")
    canonicals = {normalized(link.get("href")) for link in soup.select('link[rel="canonical"][href]')}
    if canonicals != {OFFICIAL_ENTRY}:
        raise ValueError("Harness official careers entry canonical URL changed")
    targets = set()
    for anchor in soup.find_all("a", href=True):
        if normalized(anchor.get_text(" ", strip=True)).casefold() == "view open positions":
            href = anchor["href"].strip()
            target = href if href.startswith(("http://", "https://")) else "https://www.harness.io" + href
            targets.add(target)
    if targets != {LISTING_URL}:
        raise ValueError(f"Harness official careers entry no longer links exactly to {LISTING_URL}")
    privacy = [
        anchor["href"].strip()
        for anchor in soup.select('a[href*="job-candidate-privacy"]')
        if normalized(anchor.get_text(" ", strip=True))
    ]
    return {
        "url": response_url,
        "title": title,
        "canonical": next(iter(canonicals)),
        "view_open_positions_url": LISTING_URL,
        "job_candidate_privacy_notice": privacy[0] if privacy else None,
        "job_listing_embedded_on_entry": bool(soup.select_one("#grnhse_app, .gh-main")),
    }


def parse_listing_page(html: str, response_url: str = LISTING_URL) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.harness.io" or parts.path.rstrip("/") != "/company/jobs":
        raise ValueError("Harness job listing redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at Harness" not in title or "Open Positions" not in title:
        raise ValueError("Harness job listing title changed")
    for selector in (".gh-container", ".gh-filter", ".gh-main", ".gh-view-more"):
        if len(soup.select(selector)) != 1:
            raise ValueError(f"Harness job listing no longer has exactly one {selector}")
    button = soup.select_one(".gh-view-more")
    button_label = normalized(button.get_text(" ", strip=True))
    if button_label != "View More Positions":
        raise ValueError("Harness job listing View More control changed")
    main_text = normalized(soup.select_one(".gh-main").get_text(" ", strip=True))
    if main_text not in {"Loading...", "Loading…"}:
        raise ValueError("Harness job listing no longer starts from its observed client-rendered placeholder")

    scripts = "\n".join(script.get_text() for script in soup.find_all("script") if not script.get("src"))
    scripts = unescape(scripts)
    found = []
    for match in BOARD_URL_RE.finditer(scripts):
        board, endpoint, query = match.groups()
        found.append((board, endpoint, parse_qsl(query or "", keep_blank_values=True)))
    boards = {board for board, _, _ in found}
    if boards != {BOARD_TOKEN}:
        raise ValueError(f"Harness job listing requests a changed board set: {sorted(boards)}")
    jobs_queries = [dict(query) for board, endpoint, query in found if endpoint == "jobs"]
    department_queries = [dict(query) for board, endpoint, query in found if endpoint == "departments"]
    office_queries = [dict(query) for board, endpoint, query in found if endpoint == "offices"]
    if not any(query.get("content") == "true" for query in jobs_queries):
        raise ValueError("Harness job listing no longer requests content=true from its board API")
    if not any(query.get("render_as") == "tree" for query in department_queries):
        raise ValueError("Harness job listing no longer requests its department tree")
    if not any(query.get("render_as") == "tree" for query in office_queries):
        raise ValueError("Harness job listing no longer requests its office tree")
    if not JOB_LINK_TEMPLATE.search(scripts):
        raise ValueError("Harness job listing card apply-link template changed")
    initial = re.search(r"let count=(\d+)", scripts)
    increment = re.search(r"count\+=(\d+)", scripts)
    if initial is None or increment is None:
        raise ValueError("Harness job listing View More increment changed")
    filter_names = set(re.findall(r"createDropdown\(['\"]([^'\"]+)['\"]", scripts))
    if filter_names != {"All departments", "All locations"}:
        raise ValueError(f"Harness job listing filter names changed: {sorted(filter_names)}")
    filter_ids = sorted(name.replace(" ", "_").lower() for name in filter_names)
    return {
        "url": response_url,
        "title": title,
        "board_token": BOARD_TOKEN,
        "jobs_api_url": f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/jobs",
        "departments_api_url": f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/departments",
        "offices_api_url": f"{BOARD_ORIGIN}/v1/boards/{BOARD_TOKEN}/offices",
        "jobs_api_queries": jobs_queries,
        "departments_api_queries": department_queries,
        "offices_api_queries": office_queries,
        "job_link_template": "/company/jobs/apply?gh_jid=${jobObj.job.id}",
        "view_more_label": button_label,
        "initial_card_count": int(initial.group(1)),
        "view_more_increment": int(increment.group(1)),
        "filter_names": sorted(filter_names),
        "filter_selects": filter_ids,
        "static_listing_placeholder": main_text,
        "webflow_generator": normalized(
            (soup.find("meta", {"name": "generator"}) or {}).get("content", "")
        ),
        "inline_script_sha256": sha256(scripts.encode()).hexdigest(),
    }


def parse_jobs_api(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Harness board jobs API response is not an object")
    meta, rows = value.get("meta"), value.get("jobs")
    if not isinstance(meta, dict) or not isinstance(rows, list):
        raise ValueError("Harness board jobs API response is missing meta or jobs")
    total = meta.get("total")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Harness board jobs API meta.total is not a non-negative integer")
    if len(rows) != total:
        raise ValueError(
            f"Harness board jobs API returned {len(rows)} rows for meta.total={total}; "
            "an unobserved pagination shape must not be silently capped"
        )
    parsed, seen, issues = [], set(), []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("harness_api_row_not_object")
            continue
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            issues.append(f"harness_api_row_missing_numeric_id:{identity!r}")
            continue
        identity = str(identity)
        if identity in seen:
            issues.append(f"harness_api_duplicate_id:{identity}")
            continue
        title = normalized(row.get("title"))
        if not title:
            issues.append(f"harness_api_row_missing_title:{identity}")
            continue
        company_name = normalized(row.get("company_name"))
        if company_name != "Harness":
            raise ValueError(f"Harness board row {identity} has changed employer name: {company_name!r}")
        content = row.get("content")
        if not isinstance(content, str) or len(text_html(decode_html_wrapper(content))) < 100:
            issues.append(f"harness_api_row_missing_or_short_description:{identity}")
        location = row.get("location")
        if not isinstance(location, dict) or not normalized(location.get("name")):
            issues.append(f"harness_api_row_missing_location:{identity}")
        departments = row.get("departments")
        offices = row.get("offices")
        if not isinstance(departments, list) or not departments:
            issues.append(f"harness_api_row_missing_department:{identity}")
        if not isinstance(offices, list) or not offices:
            issues.append(f"harness_api_row_missing_office:{identity}")
        _parse_iso(row.get("first_published"), f"job {identity} first_published")
        _parse_iso(row.get("updated_at"), f"job {identity} updated_at")
        _parse_iso(row.get("application_deadline"), f"job {identity} application_deadline")
        if not isinstance(row.get("pay_input_ranges", []), list):
            issues.append(f"harness_api_row_pay_ranges_not_list:{identity}")
        canonical = _canonical_apply_url(identity, row.get("absolute_url"))
        if row.get("absolute_url") != canonical and row.get("absolute_url") is not None:
            # The live board repeats gh_jid twice; the card/route is the same.
            original = row["absolute_url"]
            original_ids = [v for k, v in parse_qsl(urlsplit(original).query) if k == "gh_jid"]
            if not original_ids or any(v != identity for v in original_ids):
                raise ValueError(f"Harness board row {identity} has an unexpected absolute_url: {original!r}")
        parsed.append(dict(row))
        seen.add(identity)
    if len(parsed) != total:
        issues.append("harness_api_retained_rows_do_not_match_meta_total")
    return {
        "rows": parsed,
        "count": total,
        "job_ids": [str(row["id"]) for row in parsed],
        "issues": issues,
        "fields": sorted({key for row in parsed for key in row}),
    }


def _walk_departments(nodes, expected_ids, parent_path=()):
    job_map, nodes_out, issues = {}, [], []
    seen = set()

    def walk(node, path):
        if not isinstance(node, dict):
            issues.append("harness_department_node_not_object")
            return
        identity, name = node.get("id"), normalized(node.get("name"))
        if not isinstance(identity, int) or not name:
            issues.append(f"harness_department_node_identity_missing:{identity!r}")
            return
        current_path = path + (name,)
        jobs = node.get("jobs") or []
        if len(current_path) > 2 and jobs:
            issues.append(f"harness_department_jobs_below_observed_ui_depth:{identity}")
        if not isinstance(jobs, list):
            issues.append(f"harness_department_jobs_not_list:{identity}")
            jobs = []
        for job in jobs:
            job_id = str(job.get("id") if isinstance(job, dict) else "")
            if not job_id.isdigit():
                issues.append(f"harness_department_job_id_missing:{identity}")
                continue
            if job_id in seen:
                issues.append(f"harness_department_job_duplicate:{job_id}")
                continue
            seen.add(job_id)
            job_map[job_id] = {
                "top_level": current_path[0],
                "department": name,
                "team": current_path[-2] if len(current_path) > 1 else None,
                "path": list(current_path),
                "display": ", ".join(current_path),
                "department_id": identity,
            }
        nodes_out.append({
            "id": identity,
            "name": name,
            "path": list(current_path),
            "jobs": [str(job["id"]) for job in jobs if isinstance(job, dict) and job.get("id") is not None],
            "children": len(node.get("children") or []),
        })
        for child in node.get("children") or []:
            walk(child, current_path)

    for node in nodes or []:
        walk(node, parent_path)
    missing = sorted(set(expected_ids) - seen, key=int)
    extra = sorted(seen - set(expected_ids), key=int)
    if missing:
        issues.append(f"harness_department_tree_missing_jobs:{','.join(missing[:10])}")
    if extra:
        issues.append(f"harness_department_tree_unknown_jobs:{','.join(extra[:10])}")
    if any(len(item["path"]) > 2 and item["jobs"] for item in nodes_out):
        issues.append("harness_department_jobs_exceed_observed_two_level_ui")
    return {"job_map": job_map, "nodes": nodes_out, "issues": issues, "job_ids": sorted(seen, key=int)}


def _walk_offices(nodes, expected_ids):
    job_map, offices_out, issues = {}, [], []
    seen = set()

    def walk(node, path):
        if not isinstance(node, dict):
            issues.append("harness_office_node_not_object")
            return
        identity, name = node.get("id"), normalized(node.get("name"))
        if not isinstance(identity, int) or not name:
            issues.append(f"harness_office_node_identity_missing:{identity!r}")
            return
        current_path = path + (name,)
        record = {
            "id": identity,
            "name": name,
            "location": node.get("location"),
            "path": list(current_path),
            "jobs": [],
        }
        for department in node.get("departments") or []:
            for job in department.get("jobs") or []:
                job_id = str(job.get("id") if isinstance(job, dict) else "")
                if not job_id.isdigit():
                    issues.append(f"harness_office_job_id_missing:{identity}")
                    continue
                seen.add(job_id)
                record["jobs"].append(job_id)
                job_map.setdefault(job_id, []).append({
                    "office_id": identity,
                    "office_name": name,
                    "office_path": list(current_path),
                    "office_location": node.get("location"),
                })
        offices_out.append(record)
        for child in node.get("children") or []:
            walk(child, current_path)

    for node in nodes or []:
        walk(node, ())
    missing = sorted(set(expected_ids) - seen, key=int)
    extra = sorted(seen - set(expected_ids), key=int)
    if missing:
        issues.append(f"harness_office_tree_missing_jobs:{','.join(missing[:10])}")
    if extra:
        issues.append(f"harness_office_tree_unknown_jobs:{','.join(extra[:10])}")
    return {"job_map": job_map, "offices": offices_out, "issues": issues, "job_ids": sorted(seen, key=int)}


def _flatten_blocks(root: Tag):
    blocks = []

    def walk(node):
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.name in HEADING_TAGS:
                blocks.append(child)
                continue
            if child.name in BLOCK_TAGS:
                blocks.append(child)
                continue
            if child.name in CONTAINER_TAGS:
                has_block_child = child.find(list(HEADING_TAGS | BLOCK_TAGS | CONTAINER_TAGS), recursive=False)
                if has_block_child:
                    walk(child)
                elif normalized(child.get_text(" ", strip=True)) or child.find(["img", "a", "br"]):
                    blocks.append(child)
                continue
            if child.find(list(HEADING_TAGS | BLOCK_TAGS | CONTAINER_TAGS), recursive=False):
                walk(child)
            elif normalized(child.get_text(" ", strip=True)) or child.find(["img", "a", "br"]):
                blocks.append(child)

    walk(root)
    return blocks


def _heading_text(block: Tag) -> str:
    if block.name in HEADING_TAGS:
        return normalized(block.get_text(" ", strip=True))
    if block.name != "p":
        return ""
    text = normalized(block.get_text(" ", strip=True))
    if not text or len(text) > 100 or len(text.split()) > 12:
        return ""
    strongs = block.find_all(["strong", "b"])
    if not strongs:
        return ""
    strong_text = normalized(" ".join(node.get_text(" ", strip=True) for node in strongs))
    if strong_text and (strong_text == text or strong_text == text.rstrip(":")):
        return text.rstrip(":").strip()
    return ""


def description_sections(html: str) -> list[dict]:
    """Keep the employer-authored heading and the exact body below it."""
    decoded = decode_html_wrapper(html)
    soup = BeautifulSoup(f'<div data-harness-body="true">{decoded}</div>', "lxml")
    root = soup.select_one("[data-harness-body]")
    if root is None:
        return []
    sections, current = [], None
    for block in _flatten_blocks(root):
        heading = _heading_text(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "heading_html": str(block), "html": "", "text": ""}
            continue
        if not normalized(block.get_text(" ", strip=True)) and not block.find(["img", "a", "br"]):
            continue
        if current is None:
            current = {"heading": None, "heading_html": "", "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = text_html(section["html"]) if section["html"] else ""
    return [section for section in sections if section["text"] or section["heading"]]


def _matching_sections(sections: list[dict], pattern: re.Pattern) -> list[dict]:
    return [
        section for section in sections
        if section.get("heading") and pattern.search(section["heading"])
    ]


def _published_pay_text(description_text: str) -> str | None:
    match = PAY_TEXT.search(description_text)
    return normalized(match.group(0)) if match else None


def _salary(row: dict, description_text: str) -> dict | None:
    ranges = row.get("pay_input_ranges")
    published = _published_pay_text(description_text)
    if not isinstance(ranges, list):
        ranges = []
    if not ranges and not published:
        return None
    salary = {
        "source_field": "pay_input_ranges" if ranges else "description_content",
        "ranges": ranges,
    }
    if published:
        salary["published_text"] = published
    formatted = []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        minimum, maximum = item.get("min_cents"), item.get("max_cents")
        if isinstance(minimum, int) and isinstance(maximum, int):
            formatted.append({
                "min_cents": minimum,
                "max_cents": maximum,
                "currency": item.get("currency_type"),
                "label": item.get("title"),
                "published_range_text": f"${minimum / 100:,.0f} - ${maximum / 100:,.0f}"
                                         + (f" {item['currency_type']}" if item.get("currency_type") else ""),
            })
    if formatted:
        salary["formatted"] = formatted
    return salary


def ui_order_from_departments(nodes) -> list[str]:
    """Reproduce the observed all-jobs rendering order of Harness's own listing."""
    order = []
    for top in nodes or []:
        for job in top.get("jobs") or []:
            order.append(str(job["id"]))
        for child in top.get("children") or []:
            for job in child.get("jobs") or []:
                order.append(str(job["id"]))
            for grandchild in child.get("children") or []:
                for job in grandchild.get("jobs") or []:
                    order.append(str(job["id"]))
    return order


def _split_locations(value: str | None) -> list[str]:
    return [part.strip() for part in re.split(r"\s*;\s*", value or "") if part.strip()]


def parse_job_detail(html: str, expected_row: dict, expected_url: str) -> dict:
    side = urlsplit(expected_url)
    if side.scheme != "https" or side.netloc != "job-boards.greenhouse.io" or side.path != "/embed/job_app":
        raise ValueError("Harness representative detail URL is not the inspected Greenhouse embed route")
    query = dict(parse_qsl(side.query, keep_blank_values=True))
    if query != {"for": BOARD_TOKEN, "token": str(expected_row["id"])}:
        raise ValueError(f"Harness representative detail URL query changed: {query!r}")
    soup = BeautifulSoup(html, "lxml")
    descriptions = soup.select(".job__description")
    if len(descriptions) != 1:
        raise ValueError("Harness representative detail page no longer has exactly one .job__description")
    title_node = soup.select_one(".job__title h1") or soup.select_one("h1")
    location_node = soup.select_one(".job__location")
    if title_node is None or location_node is None:
        raise ValueError("Harness representative detail page is missing its title or location")
    rendered_title = normalized(title_node.get_text(" ", strip=True))
    rendered_location = normalized(location_node.get_text(" ", strip=True))
    expected_title = normalized(expected_row.get("title"))
    expected_locations = _split_locations((expected_row.get("location") or {}).get("name"))
    rendered_text = descriptions[0].get_text("\n", strip=True)
    source_text = text_html(decode_html_wrapper(expected_row.get("content")))
    rendered_normalized = normalize_for_compare(rendered_text)
    source_normalized = normalize_for_compare(source_text)
    form = soup.select_one(".application--container, #application_form")
    body_text = normalized(soup.get_text(" ", strip=True))
    return {
        "url": expected_url,
        "page_title": normalized(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "title": rendered_title,
        "location": rendered_location,
        "title_matches": rendered_title == expected_title,
        "location_matches": rendered_location == normalized(
            (expected_row.get("location") or {}).get("name")
        ) or rendered_location in expected_locations,
        "description_normalized_chars": len(rendered_normalized),
        "description_normalized_sha256": sha256(rendered_normalized.encode()).hexdigest(),
        "source_normalized_chars": len(source_normalized),
        "source_normalized_sha256": sha256(source_normalized.encode()).hexdigest(),
        "description_matches": rendered_normalized == source_normalized,
        "application_form_present": form is not None,
        "apply_for_this_job_present": "Apply for this job" in body_text,
        "application_question_text_in_description": "Submit application" in rendered_text,
        "description_start": rendered_normalized[:180],
    }


class Harness(Adapter):
    """Read only the exact harnessinc board requested by Harness's own listing."""

    VERIFICATION_REVISION = "harness.company-listing.greenhouse-harnessinc.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._initial_snapshot = None

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            redirects=[str(item.url) for item in response.history],
        )
        return parsed

    async def _listing_page(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    @staticmethod
    def _assert_board_response(response, endpoint: str):
        parts = urlsplit(str(response.url))
        expected_path = f"/v1/boards/{BOARD_TOKEN}/{endpoint}"
        if parts.scheme != "https" or parts.netloc != "boards-api.greenhouse.io" or parts.path != expected_path:
            raise ValueError(
                f"Harness board {endpoint} API redirected away from its company-scoped route: {response.url}"
            )

    async def _snapshot(self):
        snapshot_started = time.perf_counter()
        jobs_page_started = time.perf_counter()
        page_response = await self.http.get(JOBS_API, params={"content": "true"})
        jobs_page_seconds = max(0.0, time.perf_counter() - jobs_page_started)
        page_parsed = parse_jobs_api(page_response.json())
        self._assert_board_response(page_response, "jobs")

        pay_started = time.perf_counter()
        pay_response = await self.http.get(
            JOBS_API, params={"content": "true", "pay_transparency": "true"}
        )
        pay_seconds = max(0.0, time.perf_counter() - pay_started)
        pay_parsed = parse_jobs_api(pay_response.json())
        self._assert_board_response(pay_response, "jobs")

        departments_started = time.perf_counter()
        departments_response = await self.http.get(DEPARTMENTS_API, params={"render_as": "tree"})
        departments_seconds = max(0.0, time.perf_counter() - departments_started)
        self._assert_board_response(departments_response, "departments")
        department_value = departments_response.json()
        if not isinstance(department_value, dict) or not isinstance(department_value.get("departments"), list):
            raise ValueError("Harness departments API response changed")

        offices_started = time.perf_counter()
        offices_response = await self.http.get(OFFICES_API, params={"render_as": "tree"})
        offices_seconds = max(0.0, time.perf_counter() - offices_started)
        self._assert_board_response(offices_response, "offices")
        office_value = offices_response.json()
        if not isinstance(office_value, dict) or not isinstance(office_value.get("offices"), list):
            raise ValueError("Harness offices API response changed")

        issues = list(page_parsed["issues"]) + list(pay_parsed["issues"])
        if page_parsed["count"] != pay_parsed["count"] or page_parsed["job_ids"] != pay_parsed["job_ids"]:
            issues.append("harness_page_exact_and_pay_api_membership_mismatch")
        if len(page_parsed["rows"]) != len(pay_parsed["rows"]):
            issues.append("harness_page_exact_and_pay_api_row_count_mismatch")
        common_mismatch = []
        for left, right in zip(page_parsed["rows"], pay_parsed["rows"]):
            for key in sorted(set(left) | set(right)):
                if key == "pay_input_ranges":
                    continue
                if left.get(key) != right.get(key):
                    common_mismatch.append({"id": str(left.get("id")), "field": key})
        if common_mismatch:
            issues.append("harness_page_exact_and_pay_api_common_fields_changed")

        departments = _walk_departments(department_value["departments"], pay_parsed["job_ids"])
        offices = _walk_offices(office_value["offices"], pay_parsed["job_ids"])
        issues.extend(departments["issues"])
        issues.extend(offices["issues"])

        # The rendered page iterates departments in tree order, emits parent jobs
        # then child jobs. This is the exact order of the observed cards.
        ui_order = ui_order_from_departments(department_value["departments"])
        if len(ui_order) != len(set(ui_order)):
            issues.append("harness_department_ui_order_contains_duplicate_jobs")
        if set(ui_order) != set(pay_parsed["job_ids"]):
            issues.append("harness_department_ui_order_does_not_match_api_membership")

        rows = []
        pay_by_id = {str(row["id"]): row for row in pay_parsed["rows"]}
        for row in page_parsed["rows"]:
            merged = dict(row)
            pay_row = pay_by_id.get(str(row["id"]), {})
            merged["pay_input_ranges"] = pay_row.get("pay_input_ranges") or row.get("pay_input_ranges") or []
            rows.append(merged)
        rows_by_id = {str(row["id"]): row for row in rows}
        ordered_rows = [rows_by_id[job_id] for job_id in ui_order]

        return {
            "rows": ordered_rows,
            "count": pay_parsed["count"],
            "job_ids": pay_parsed["job_ids"],
            "ui_order": ui_order,
            "page_exact": {
                "url": str(page_response.request.url),
                "final_url": str(page_response.url),
                "status": page_response.status_code,
                "seconds": jobs_page_seconds,
                "response_bytes": len(page_response.content),
                "response_sha256": sha256(page_response.content).hexdigest(),
                "count": page_parsed["count"],
                "job_ids": page_parsed["job_ids"],
                "fields": page_parsed["fields"],
            },
            "pay": {
                "url": str(pay_response.request.url),
                "final_url": str(pay_response.url),
                "status": pay_response.status_code,
                "seconds": pay_seconds,
                "response_bytes": len(pay_response.content),
                "response_sha256": sha256(pay_response.content).hexdigest(),
                "count": pay_parsed["count"],
                "job_ids": pay_parsed["job_ids"],
                "fields": pay_parsed["fields"],
            },
            "departments": {
                **departments,
                "url": str(departments_response.request.url),
                "final_url": str(departments_response.url),
                "status": departments_response.status_code,
                "seconds": departments_seconds,
                "response_bytes": len(departments_response.content),
                "response_sha256": sha256(departments_response.content).hexdigest(),
                "top_level_nodes": len(department_value["departments"]),
            },
            "offices": {
                **offices,
                "url": str(offices_response.request.url),
                "final_url": str(offices_response.url),
                "status": offices_response.status_code,
                "seconds": offices_seconds,
                "response_bytes": len(offices_response.content),
                "response_sha256": sha256(offices_response.content).hexdigest(),
                "top_level_nodes": len(office_value["offices"]),
            },
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": {
                "jobs_page": jobs_page_seconds,
                "jobs_pay": pay_seconds,
                "departments": departments_seconds,
                "offices": offices_seconds,
            },
            "snapshot_wall_seconds": max(0.0, time.perf_counter() - snapshot_started),
            "common_field_mismatches": common_mismatch,
        }

    def _make_job(self, row: dict, snapshot: dict):
        identity = str(row["id"])
        department = snapshot["departments"]["job_map"].get(identity)
        if department is None:
            raise ValueError(f"Harness job {identity} is absent from the department tree")
        office_records = snapshot["offices"]["job_map"].get(identity) or []
        description_html = decode_html_wrapper(row.get("content"))
        description_text = text_html(description_html)
        sections = description_sections(row.get("content"))
        responsibilities = _matching_sections(sections, RESPONSIBILITY_HEADING)
        qualifications = _matching_sections(sections, QUALIFICATION_HEADING)
        locations = _split_locations((row.get("location") or {}).get("name"))
        job = self.job(
            {"listing": row, "department_hierarchy": department, "office_hierarchy": office_records},
            title=normalized(row.get("title")),
            url=_canonical_apply_url(identity, row.get("absolute_url")),
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=_canonical_apply_url(identity, row.get("absolute_url")),
            description=description_html,
            method="harness.company_apply_page.greenhouse_embed_job_app.job__description",
            description_url=detail_embed_url(identity),
            locations=locations,
            department=department["display"],
            team=department["team"],
            employment_type=None,
            workplace_type=None,
            education=row.get("education"),
            date_posted=row.get("first_published"),
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            valid_through=row.get("application_deadline"),
            salary=_salary(row, description_text),
            employer=normalized(row.get("company_name")),
            language=row.get("language"),
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
        )
        job.description_text = description_text
        job.raw["detail_url"] = detail_embed_url(identity)
        job.raw["description_section_headings"] = [
            section["heading"] for section in sections if section["heading"]
        ]
        job.raw["description_sections"] = sections
        job.raw["pay_input_ranges"] = row.get("pay_input_ranges") or []
        job.raw["source_fields"] = sorted(row)
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def _detail(self, job):
        row = job.raw["listing"]
        started = time.perf_counter()
        response = await self.http.get(job.raw["detail_url"])
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_job_detail(response.text, row, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        job.raw["detail"] = parsed
        self.result.timings["detail_fetch_seconds_sum"] = (
            self.result.timings.get("detail_fetch_seconds_sum", 0.0) + seconds
        )
        if not parsed["description_matches"]:
            job.warnings.extend(["detail_render_mismatch", "description_not_full_field"])
            self.result.errors.append(f"Harness detail body mismatch: {job.job_id} {job.url}")
        if not parsed["title_matches"]:
            job.warnings.append("detail_title_mismatch")
            self.result.errors.append(f"Harness detail title mismatch: {job.job_id} {job.url}")
        if not parsed["location_matches"]:
            job.warnings.append("detail_location_mismatch")
            self.result.errors.append(f"Harness detail location mismatch: {job.job_id} {job.url}")
        if not parsed["application_form_present"] or not parsed["apply_for_this_job_present"]:
            job.warnings.append("application_route_not_observed")
            self.result.errors.append(f"Harness application route missing: {job.job_id} {job.url}")
        return job

    def _membership_reconciliation(
        self, initial: dict, final: dict, entry: dict, listing: dict, initial_listing: dict
    ) -> dict:
        initial_rows = {str(row["id"]): row for row in initial["rows"]}
        final_rows = {str(row["id"]): row for row in final["rows"]}
        changed_fields = {}
        for identity in initial_rows:
            if identity not in final_rows:
                continue
            left, right = initial_rows[identity], final_rows[identity]
            fields = sorted(
                key for key in set(left) | set(right)
                if left.get(key) != right.get(key)
            )
            if fields:
                changed_fields[identity] = fields
        same_id_set = set(initial["job_ids"]) == set(final["job_ids"])
        same_api_order = initial["job_ids"] == final["job_ids"]
        same_ui_order = initial["ui_order"] == final["ui_order"]
        same_counts = initial["count"] == final["count"]
        same_page_exact = set(initial["page_exact"]["job_ids"]) == set(final["page_exact"]["job_ids"])
        same_pay = set(initial["pay"]["job_ids"]) == set(final["pay"]["job_ids"])
        same_department_mapping = initial["departments"]["job_map"] == final["departments"]["job_map"]
        same_office_mapping = initial["offices"]["job_map"] == final["offices"]["job_map"]
        entry_same = (
            entry.get("view_open_positions_url") == listing.get("url") == LISTING_URL
            and entry.get("canonical") == OFFICIAL_ENTRY
        )
        listing_contract_same = all(
            initial["departments"].get(key) == final["departments"].get(key)
            for key in ("top_level_nodes",)
        ) and all(
            initial["offices"].get(key) == final["offices"].get(key)
            for key in ("top_level_nodes",)
        ) and all(
            initial_listing.get(key) == listing.get(key)
            for key in (
                "board_token", "jobs_api_url", "departments_api_url", "offices_api_url",
                "job_link_template", "initial_card_count", "view_more_increment",
                "filter_selects",
            )
        )
        complete = all([
            same_id_set, same_ui_order, same_counts, same_page_exact, same_pay,
            same_department_mapping, same_office_mapping, entry_same,
            listing_contract_same, not changed_fields,
            not initial["issues"], not final["issues"],
        ])
        return {
            "initial_count": initial["count"],
            "final_count": final["count"],
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"],
            "initial_ui_order": initial["ui_order"],
            "final_ui_order": final["ui_order"],
            "same_id_set": same_id_set,
            "same_api_order": same_api_order,
            "same_ui_order": same_ui_order,
            "same_counts": same_counts,
            "same_page_exact_api_ids": same_page_exact,
            "same_pay_api_ids": same_pay,
            "same_department_mapping": same_department_mapping,
            "same_office_mapping": same_office_mapping,
            "official_entry_still_points_to_listing": entry_same,
            "listing_tree_contract_same": listing_contract_same,
            "all_row_fields_match": not changed_fields,
            "changed_fields_by_job_id": changed_fields,
            "new_job_ids": [i for i in final["job_ids"] if i not in initial["job_ids"]],
            "removed_job_ids": [i for i in initial["job_ids"] if i not in final["job_ids"]],
            "initial_issues": initial["issues"],
            "final_issues": final["issues"],
            "complete": complete,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "board_token": BOARD_TOKEN,
            "listing_authority": (
                "Harness's own /company/jobs Webflow page fetches the harnessinc "
                "Greenhouse board API directly and renders all of its jobs as cards."
            ),
            "request_scope": (
                "Only www.harness.io and the exact harnessinc board routes requested "
                "by Harness's listing page; no other Greenhouse board is queried."
            ),
            "detail_method": (
                "The company card route /company/jobs/apply?gh_jid=ID embeds "
                "job-boards.greenhouse.io/embed/job_app?for=harnessinc&token=ID. "
                "Full .job__description is captured/verified there; pay ranges come "
                "from the same harnessinc jobs API with pay_transparency=true."
            ),
        })
        entry = listing = initial = None
        try:
            entry = await self._official_entry()
            listing = await self._listing_page()
            initial = await self._snapshot()
        except Exception as exc:
            self.result.errors.append(f"Harness source collection failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result
        source_initial_seconds = max(0.0, time.perf_counter() - source_started)
        self.result.expected_total = initial["count"]
        self.result.pages = 1
        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["initial_listing_page"] = listing
        self.result.metadata["initial_api_listing"] = {
            "page_exact": initial["page_exact"],
            "pay": initial["pay"],
            "common_field_mismatches": initial["common_field_mismatches"],
        }
        self.result.metadata["initial_departments"] = {
            key: value for key, value in initial["departments"].items() if key != "job_map"
        }
        self.result.metadata["initial_offices"] = {
            key: value for key, value in initial["offices"].items() if key != "job_map"
        }
        self.result.metadata["ui_order"] = initial["ui_order"]
        self.result.metadata["ui_order_source"] = (
            "Department tree traversal used by the observed .gh-main renderer; "
            "independently compared to the headless-browser card order in run.py audit."
        )
        self.result.metadata["initial_pay_range_count"] = sum(
            bool(row.get("pay_input_ranges")) for row in initial["rows"]
        )
        self.result.metadata["initial_request_seconds"] = initial["request_seconds"]
        self.result.timings["initial_source_collection_wall_seconds"] = source_initial_seconds
        self.result.timings["initial_jobs_page_api_seconds"] = initial["request_seconds"]["jobs_page"]
        self.result.timings["initial_jobs_pay_api_seconds"] = initial["request_seconds"]["jobs_pay"]
        self.result.timings["initial_departments_api_seconds"] = initial["request_seconds"]["departments"]
        self.result.timings["initial_offices_api_seconds"] = initial["request_seconds"]["offices"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])

        jobs = []
        row_by_id = {str(row["id"]): row for row in initial["rows"]}
        for identity in initial["ui_order"]:
            row = row_by_id.get(identity)
            if row is None:
                self.result.errors.append(f"harness_listing_card_missing_api_row:{identity}")
                continue
            try:
                job = self._make_job(row, initial)
            except Exception as exc:
                self.result.errors.append(f"harness_job_parse_failed:{identity}: {type(exc).__name__}: {exc}")
                continue
            self.add(job)
            jobs.append(job)
        if len(jobs) != initial["count"]:
            self.result.errors.append("harness_retained_rows_do_not_match_listing_count")

        await self.hydrate(jobs, self._detail)
        self.result.metadata["detail_hydration"] = self.result.metadata.get("details", {})
        if any("detail" not in job.raw for job in self.result.jobs):
            self.result.errors.append("harness_detail_verification_incomplete")

        final = None
        final_started = time.perf_counter()
        try:
            final_entry = await self._official_entry()
            final_listing = await self._listing_page()
            final = await self._snapshot()
        except Exception as exc:
            self.result.errors.append(f"Harness final source recheck failed: {type(exc).__name__}: {exc}")
            final_entry = final_listing = None
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_source_recheck_wall_seconds"] = final_seconds
        if final is None or final_entry is None or final_listing is None:
            reconciliation = {"complete": False, "reason": "final_recheck_failed"}
        else:
            reconciliation = self._membership_reconciliation(
                initial, final, final_entry, final_listing, listing
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing_page"] = final_listing
            self.result.metadata["final_api_listing"] = {
                "page_exact": final["page_exact"],
                "pay": final["pay"],
                "common_field_mismatches": final["common_field_mismatches"],
            }
            self.result.metadata["final_departments"] = {
                key: value for key, value in final["departments"].items() if key != "job_map"
            }
            self.result.metadata["final_offices"] = {
                key: value for key, value in final["offices"].items() if key != "job_map"
            }
            if not reconciliation["same_id_set"]:
                self.result.errors.append("harness_listing_membership_changed_during_collection")
            if not reconciliation["same_ui_order"]:
                self.result.errors.append("harness_listing_ui_order_changed_during_collection")
            if not reconciliation["same_department_mapping"]:
                self.result.errors.append("harness_department_tree_changed_during_collection")
            if not reconciliation["same_office_mapping"]:
                self.result.errors.append("harness_office_tree_changed_during_collection")
            if not reconciliation["all_row_fields_match"]:
                self.result.errors.append("harness_source_fields_changed_during_collection")
            if not reconciliation["official_entry_still_points_to_listing"]:
                self.result.errors.append("harness_official_entry_changed_during_collection")
            if not reconciliation["same_page_exact_api_ids"] or not reconciliation["same_pay_api_ids"]:
                self.result.errors.append("harness_page_exact_or_pay_api_membership_changed")
            self.result.errors.extend(final["issues"])
        self.result.metadata["membership_reconciliation"] = reconciliation
        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["current_native_membership_reconciled"] = bool(
            reconciliation.get("complete")
        )
        self.result.metadata["description_verification"] = {
            "jobs": len(self.result.jobs),
            "verified": verified,
            "unverified": len(self.result.jobs) - verified,
            "detail_pages_compared": sum("detail" in job.raw for job in self.result.jobs),
        }
        self.result.listing_complete = bool(
            reconciliation.get("complete")
            and len(self.result.jobs) == initial["count"]
            and verified == len(self.result.jobs)
        )
        self.result.timings["source_collection_wall_seconds"] = source_initial_seconds
        self.result.timings["total_adapter_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
