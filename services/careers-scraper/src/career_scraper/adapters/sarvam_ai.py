"""Sarvam AI's own careers page, its Astro-embedded job index and exact Ashby-linked details."""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.sarvam.ai/careers"
DETAIL_URL_TEMPLATE = "https://www.sarvam.ai/careers/jobs/{job_id}"
ASHBY_BOARD = "sarvam"
ASHBY_ORIGIN = "https://jobs.ashbyhq.com"
ASHBY_BOARD_API = (
    f"https://api.ashbyhq.com/posting-api/job-board/{ASHBY_BOARD}?includeCompensation=true"
)
BRAND = "Sarvam AI"
EXPECTED_LEGAL_EMPLOYER = "Axonwise Private Limited"
EXPECTED_API_VERSION = "1"

UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
DETAIL_PATH = re.compile(r"^/careers/jobs/([0-9a-fA-F-]{36})$")
LISTING_COMPONENT = re.compile(r"^/_astro/JobListingSection\.[\w-]+\.js$")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
TOTAL_HEADING = re.compile(r"^(\d+)\s+Open Positions?$", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
CF_EMAIL_MASK = "[email protected]"

EMPLOYMENT_LABELS = {"full_time": "Full Time", "part_time": "Part Time", "intern": "Internship",
                     "contract": "Contract", "temporary": "Temporary"}
WORKPLACE_LABELS = {"on_site": "On-Site", "remote": "Remote", "hybrid": "Hybrid"}
ASHBY_EMPLOYMENT = {"FullTime": "full_time", "PartTime": "part_time", "Intern": "intern",
                    "Contract": "contract", "Temporary": "temporary"}
ASHBY_WORKPLACE = {"OnSite": "on_site", "Remote": "remote", "Hybrid": "hybrid"}
RESPONSIBILITY_TOKENS = ("what you'll do", "what you’ll do", "what you will do", "what you'll own",
                         "what you’ll own", "responsibilit", "the role")
QUALIFICATION_TOKENS = ("who you are", "what we're looking for", "what we’re looking for",
                        "what we look for", "requirements", "qualifications", "good to have",
                        "nice to have", "bonus points", "strong pluses", "basic qualifications",
                        "preferred qualifications", "about you")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def comparison_text(html: str | None) -> str:
    """Case/punctuation-insensitive body text used only for source cross-checks."""
    value = re.sub(r"[^0-9A-Za-z\u00c0-\u024f'’\-]+", " ", normalized_body_text(html)).lower()
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_astro(node):
    """Decode Astro's compact [tag, value] island serialization."""
    if isinstance(node, list):
        if len(node) == 2 and isinstance(node[0], int):
            tag, value = node
            if tag == 0:
                return decode_astro(value)
            if tag == 1:
                return [decode_astro(item) for item in value]
            if tag == 2:
                return {str(key): decode_astro(item) for key, item in value}
            return value
        return [decode_astro(item) for item in node]
    if isinstance(node, dict):
        return {key: decode_astro(value) for key, value in node.items()}
    return node


def _sr_job_row(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "department": raw.get("department"),
        "location": raw.get("location"),
        "workplace": raw.get("workplace"),
        "workplace_type": WORKPLACE_LABELS.get(raw.get("workplace")),
        "employment_type": raw.get("employmentType"),
        "employment_label": EMPLOYMENT_LABELS.get(raw.get("employmentType")),
        "status": raw.get("status"),
        "apply_url": raw.get("applyUrl"),
        "description": raw.get("description"),
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
    }


def parse_careers_listing(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the company-published Astro job index and its rendered cards."""
    parsed_url = urlsplit(response_url)
    if (parsed_url.scheme != "https" or parsed_url.netloc != "www.sarvam.ai"
            or parsed_url.path.rstrip("/") != "/careers" or parsed_url.query):
        raise ValueError("Sarvam AI careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if page_title != "Careers | Sarvam AI":
        raise ValueError("Sarvam AI careers page title changed")
    islands = [
        node for node in soup.select("astro-island[component-url]")
        if LISTING_COMPONENT.fullmatch(node.get("component-url", ""))
    ]
    if len(islands) != 1:
        raise ValueError("Sarvam AI careers page no longer exposes one job-listing island")
    island = islands[0]
    if island.get("component-export") != "default":
        raise ValueError("Sarvam AI job-listing island export changed")
    props = json.loads(island.get("props", "{}"))
    raw_jobs = decode_astro(props.get("allJobs"))
    departments = decode_astro(props.get("departments")) or []
    locations = decode_astro(props.get("locations")) or []
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("Sarvam AI job-listing island has no jobs")
    if not isinstance(departments, list) or not isinstance(locations, list):
        raise ValueError("Sarvam AI job-listing filter options changed shape")

    issues, rows, seen = [], [], set()
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            issues.append("sarvam_listing_row_not_object")
            continue
        row = _sr_job_row(raw)
        identity = str(row["id"] or "")
        if not UUID.fullmatch(identity):
            issues.append(f"sarvam_listing_row_missing_uuid:{identity[:40]}")
            continue
        if identity in seen:
            issues.append(f"sarvam_listing_duplicate_id:{identity}")
            continue
        if normalized_title(row["title"]) == "" or row["description"] is not None:
            issues.append(f"sarvam_listing_row_fields_changed:{identity}")
            continue
        if row["status"] != "open":
            issues.append(f"sarvam_listing_row_not_open:{identity}")
            continue
        if not row["employment_label"] or not row["workplace_type"]:
            issues.append(f"sarvam_listing_row_type_changed:{identity}")
            continue
        if row["department"] not in departments or row["location"] not in locations:
            issues.append(f"sarvam_listing_row_option_unknown:{identity}")
            continue
        if row["apply_url"] != f"{ASHBY_ORIGIN}/{ASHBY_BOARD}/{identity}/application":
            issues.append(f"sarvam_listing_row_apply_url_changed:{identity}")
            continue
        if not DATE_ONLY.fullmatch(str(row["created_at"])) or not ISO_TIMESTAMP.fullmatch(str(row["updated_at"])):
            issues.append(f"sarvam_listing_row_dates_changed:{identity}")
            continue
        seen.add(identity)
        rows.append(row)
    props_by_id = {row["id"]: row for row in rows}

    rendered, rendered_seen = [], set()
    for anchor in soup.select('a[href^="/careers/jobs/"]'):
        match = DETAIL_PATH.fullmatch(anchor.get("href", ""))
        if not match:
            continue
        identity = match.group(1).lower()
        if identity in rendered_seen:
            issues.append(f"sarvam_rendered_duplicate_card:{identity}")
            continue
        rendered_seen.add(identity)
        title_node = anchor.select_one("h3")
        meta_node = anchor.select_one("p")
        labels = [
            node.get_text(" ", strip=True)
            for node in (meta_node.select("p > span > span:last-child") if meta_node else [])
            if node.get_text(strip=True)
        ]
        if title_node is None or len(labels) != 3:
            issues.append(f"sarvam_rendered_card_fields_missing:{identity}")
            continue
        rendered.append({
            "id": identity,
            "title": normalized_title(title_node.get_text(" ", strip=True)),
            "location": labels[0],
            "employment_label": labels[1],
            "workplace_label": labels[2],
            "href": anchor.get("href"),
        })

    groups = []
    for heading in soup.select("h3"):
        if "uppercase" not in " ".join(heading.get("class") or []):
            continue
        container = heading.find_parent("div")
        cards = container.find_next_sibling("div") if container else None
        anchors = cards.select('a[href^="/careers/jobs/"]') if cards else []
        if not anchors:
            continue
        badge = heading.find_next_sibling()
        groups.append({
            "label": normalized_title(heading.get_text(" ", strip=True)),
            "badge_count": badge.get_text(" ", strip=True) if badge else None,
            "card_count": len(anchors),
        })

    headings = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h2")]
    total_headings = [int(match.group(1)) for value in headings if (match := TOTAL_HEADING.fullmatch(value))]
    if len(total_headings) != 1:
        issues.append("sarvam_listing_total_heading_missing")
    elif total_headings[0] != len(rows):
        issues.append(f"sarvam_listing_total_heading_mismatch:{total_headings[0]}!={len(rows)}")

    if len(rendered) != len(rows):
        issues.append(f"sarvam_rendered_card_count_mismatch:{len(rendered)}!={len(rows)}")
    rendered_by_id = {card["id"]: card for card in rendered}
    if set(rendered_by_id) != set(props_by_id):
        issues.append("sarvam_rendered_and_embedded_job_ids_differ")
    for identity, card in rendered_by_id.items():
        row = props_by_id.get(identity)
        if row is None:
            continue
        if (card["title"] != normalized_title(row["title"])
                or card["location"] != row["location"]
                or card["employment_label"] != row["employment_label"]
                or card["workplace_label"] != row["workplace_type"]):
            issues.append(f"sarvam_rendered_card_fields_differ:{identity}")

    for group in groups:
        if re.fullmatch(r"\d+", group["badge_count"] or "") and int(group["badge_count"]) != group["card_count"]:
            issues.append(f"sarvam_department_badge_mismatch:{group['label']}")
    group_total = sum(group["card_count"] for group in groups)
    if groups and group_total != len(rows):
        issues.append(f"sarvam_department_groups_do_not_cover_listing:{group_total}!={len(rows)}")
    props_department_counts = {}
    for row in rows:
        key = normalized_title(row["department"])
        props_department_counts[key] = props_department_counts.get(key, 0) + 1
    rendered_department_counts = {normalized_title(group["label"]): group["card_count"] for group in groups}
    if props_department_counts != rendered_department_counts:
        issues.append("sarvam_department_group_counts_differ_from_embedded_rows")

    search_input = soup.select_one('input[type="search"][placeholder="Search roles..."]')
    selects = soup.select("select")
    pagination = [
        node.get_text(" ", strip=True) for node in soup.select("button,a")
        if re.search(r"\b(?:next|previous|load more|show more)\b", node.get_text(" ", strip=True), re.I)
    ]

    return {
        "url": response_url,
        "page_title": page_title,
        "rows": rows,
        "props_job_ids": [row["id"] for row in rows],
        "rows_by_id": props_by_id,
        "rendered_rows": rendered,
        "job_ids": [card["id"] for card in rendered],
        "count": len(rows),
        "department_groups": groups,
        "departments": departments,
        "locations": locations,
        "initial_query": {
            "q": decode_astro(props.get("initialQ")),
            "department": decode_astro(props.get("initialDepartment")),
            "location": decode_astro(props.get("initialLocation")),
        },
        "filters": {
            "keyword_search_input": bool(search_input),
            "select_count": len(selects),
            "client_side_only": True,
            "url_query_parameters": ["q", "department", "location"],
        },
        "pagination_controls": pagination,
        "total_heading": total_headings[0] if len(total_headings) == 1 else None,
        "component_url": island.get("component-url"),
        "issues": list(dict.fromkeys(issues)),
    }


def assert_listing_component(script: str, component_url: str) -> dict:
    """Prove the shipped listing component filters the embedded rows without paging."""
    if "allJobs" not in script or "department" not in script or "location" not in script:
        raise ValueError("Sarvam AI listing component no longer matches the inspected filter behaviour")
    if "fetch(" in script or "XMLHttpRequest" in script:
        raise ValueError("Sarvam AI listing component now fetches jobs client-side; pagination must be re-inspected")
    if re.search(r"(?:load|show)\s*more|pagination|nextPage|perPage|itemsPerPage", script, re.I):
        raise ValueError("Sarvam AI listing component exposes pagination controls that were not reviewed")
    if "/careers?" not in script:
        raise ValueError("Sarvam AI listing component no longer writes its inspected query parameters")
    return {
        "component_url": component_url,
        "contains_embedded_all_jobs": True,
        "client_side_fetch": False,
        "pagination_terms": False,
        "query_parameters": ["q", "department", "location"],
        "script_sha256": sha256(script.encode()).hexdigest(),
        "script_bytes": len(script.encode()),
    }


def parse_ashby_board(value: dict) -> dict:
    """Validate the company-scoped Ashby board that the careers cards link to."""
    if not isinstance(value, dict) or str(value.get("apiVersion", "")) != EXPECTED_API_VERSION:
        raise ValueError("Sarvam AI Ashby board API version changed")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Sarvam AI Ashby board returned no jobs")
    issues, rows, seen = [], [], set()
    for row in jobs:
        if not isinstance(row, dict):
            issues.append("sarvam_board_row_not_object")
            continue
        identity = str(row.get("id", ""))
        if not UUID.fullmatch(identity):
            issues.append("sarvam_board_row_missing_uuid")
            continue
        if identity in seen:
            issues.append(f"sarvam_board_duplicate_id:{identity}")
            continue
        required = ("title", "department", "team", "employmentType", "location", "publishedAt",
                    "workplaceType", "jobUrl", "applyUrl", "descriptionHtml", "descriptionPlain")
        if any(key not in row for key in required):
            issues.append(f"sarvam_board_row_fields_missing:{identity}")
            continue
        if row["applyUrl"] != f"{ASHBY_ORIGIN}/{ASHBY_BOARD}/{identity}/application":
            issues.append(f"sarvam_board_apply_url_changed:{identity}")
            continue
        if row["jobUrl"] != f"{ASHBY_ORIGIN}/{ASHBY_BOARD}/{identity}":
            issues.append(f"sarvam_board_job_url_changed:{identity}")
            continue
        if row.get("isListed") is not True or not isinstance(row.get("isRemote"), bool):
            issues.append(f"sarvam_board_row_not_listed:{identity}")
            continue
        if row["employmentType"] not in ASHBY_EMPLOYMENT or row["workplaceType"] not in ASHBY_WORKPLACE:
            issues.append(f"sarvam_board_row_type_changed:{identity}")
            continue
        if not isinstance(row.get("secondaryLocations"), list) or not isinstance(row.get("address"), dict):
            issues.append(f"sarvam_board_row_locations_changed:{identity}")
            continue
        if (len(normalized_body_text(row["descriptionHtml"])) < 100
                or len(re.sub(r"\s+", " ", row["descriptionPlain"] or "").strip()) < 100):
            issues.append(f"sarvam_board_description_empty:{identity}")
        seen.add(identity)
        rows.append(dict(row))
    return {
        "api_version": value.get("apiVersion"),
        "rows": rows,
        "jobs_by_id": {row["id"]: row for row in rows},
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    """Keep every employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-sarvam-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-sarvam-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
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
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        section["text"] = text_html(section["html"])
    return sections


def _section_matches(sections: list[dict], tokens: tuple[str, ...]) -> list[dict]:
    return [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower() for token in tokens)
    ]


def _locations(board_row: dict) -> list[dict]:
    address = (board_row.get("address") or {}).get("postalAddress") or {}
    values = [{
        "name": board_row.get("location"),
        "postal_address": address,
        "workplace_type": board_row.get("workplaceType"),
        "is_remote": board_row.get("isRemote"),
    }]
    for secondary in board_row.get("secondaryLocations") or []:
        values.append({"name": secondary.get("location"), "secondary": dict(secondary)})
    return values


def parse_detail_page(html: str, row: dict, board_row: dict, expected_url: str) -> dict:
    """Check one exact company job page and keep its full JobPosting description."""
    parts = urlsplit(expected_url)
    match = DETAIL_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != "www.sarvam.ai" or match is None
            or match.group(1).lower() != str(row["id"]).lower()):
        raise ValueError(f"Sarvam AI detail URL is not an exact exposed job link: {expected_url}")
    soup = BeautifulSoup(html, "lxml")
    expected_title = normalized_title(row["title"])
    title_node = soup.select_one("h1")
    h1 = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
    if h1 != expected_title:
        raise ValueError(f"Sarvam AI detail title changed for {row['id']}: {h1!r}")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or canonical.get("href") != expected_url:
        raise ValueError(f"Sarvam AI detail canonical URL changed for {row['id']}")

    scripts = [json.loads(node.string) for node in soup.select('script[type="application/ld+json"]')
               if node.string]
    postings = [item for item in scripts if item.get("@type") == "JobPosting"]
    if len(postings) != 1:
        raise ValueError(f"Sarvam AI detail page exposes {len(postings)} JobPosting records for {row['id']}")
    posting = postings[0]
    organization = next((item for item in scripts if item.get("@type") == "Organization"), {})
    if organization.get("name") != BRAND or organization.get("legalName") != EXPECTED_LEGAL_EMPLOYER:
        raise ValueError("Sarvam AI organization identity or legal employer changed")
    if normalized_title(posting.get("title")) != expected_title:
        raise ValueError(f"Sarvam AI structured title differs for {row['id']}")
    if posting.get("hiringOrganization", {}).get("name") != BRAND:
        raise ValueError(f"Sarvam AI hiring organization changed for {row['id']}")
    if posting.get("employmentType") != "FULL_TIME":
        raise ValueError(f"Sarvam AI employment type changed for {row['id']}")
    locality = ((posting.get("jobLocation") or {}).get("address") or {}).get("addressLocality")
    if locality != row["location"]:
        raise ValueError(f"Sarvam AI detail location differs for {row['id']}: {locality!r}")
    if posting.get("datePosted") != str(board_row.get("publishedAt", ""))[:10]:
        raise ValueError(f"Sarvam AI detail posting date differs for {row['id']}")

    description_html = posting.get("description") or ""
    if len(normalized_body_text(description_html)) < 100:
        raise ValueError(f"Sarvam AI detail description is empty for {row['id']}")
    board_text = comparison_text(board_row.get("descriptionHtml"))
    detail_text = comparison_text(description_html)
    if detail_text != board_text:
        raise ValueError(f"Sarvam AI detail description differs from its linked board row for {row['id']}")

    wrapper = soup.select_one("main > div")
    sections_nodes = wrapper.find_all("section", recursive=False) if wrapper else []
    rendered = normalized_body_text(str(sections_nodes[1])) if len(sections_nodes) > 1 else ""
    expected_body = normalized_body_text(description_html)
    rendered_matches = rendered == expected_body
    email_mask_only = False
    if not rendered_matches and CF_EMAIL_MASK in rendered:
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", expected_body)
        email_mask_only = bool(emails) and rendered.replace(CF_EMAIL_MASK, emails[0]) == expected_body
    if not (rendered_matches or email_mask_only):
        raise ValueError(f"Sarvam AI rendered body does not match its JobPosting description for {row['id']}")

    apply_links = [
        anchor.get("href") for anchor in soup.select('main a[href^="https://jobs.ashbyhq.com/"]')
    ]
    if not apply_links or set(apply_links) != {row["apply_url"]}:
        raise ValueError(f"Sarvam AI detail apply route changed for {row['id']}: {apply_links}")

    description_soup = BeautifulSoup(description_html, "lxml")
    links = [{"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
             for anchor in description_soup.select("a[href]")]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    sections = description_sections(description_html)
    responsibilities = _section_matches(sections, RESPONSIBILITY_TOKENS)
    qualifications = _section_matches(sections, QUALIFICATION_TOKENS)
    crumbs = next((item for item in scripts if item.get("@type") == "BreadcrumbList"), {})
    crumb_names = [entry.get("name") for entry in crumbs.get("itemListElement", [])]
    if crumb_names[:2] != ["Home", "Careers"] or normalized_title(crumb_names[-1] if crumb_names else "") != expected_title:
        raise ValueError(f"Sarvam AI detail breadcrumb changed for {row['id']}")

    body = normalized_body_text(description_html)
    return {
        "url": expected_url,
        "title": h1,
        "canonical_url": canonical.get("href"),
        "description_html": description_html,
        "description_text": text_html(description_html),
        "description_normalized_chars": len(body),
        "description_fnv1a64": fnv1a64(body),
        "rendered_body_normalized_chars": len(rendered),
        "rendered_body_fnv1a64": fnv1a64(rendered),
        "rendered_matches_jobposting_description": rendered_matches,
        "rendered_differs_only_by_cloudflare_email_mask": email_mask_only,
        "matches_linked_board_description": True,
        "date_posted": posting.get("datePosted"),
        "employment_type": "Full Time",
        "workplace_type": WORKPLACE_LABELS.get(
            ASHBY_WORKPLACE.get(board_row.get("workplaceType")), row.get("workplace_type")),
        "locations": _locations(board_row),
        "employer": {
            "brand": organization.get("name"),
            "legal_name": organization.get("legalName"),
            "legal_name_source": expected_url,
            "same_as": organization.get("sameAs"),
            "logo": organization.get("logo"),
        },
        "apply_url": apply_links[0],
        "breadcrumb": crumb_names,
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "description_links": links,
        "role_attachments": attachments,
        "application_form_excluded": True,
        "page_meta_description": (soup.select_one('meta[name="description"]') or {}).get("content")
        if soup.select_one('meta[name="description"]') else None,
    }


class SarvamAI(Adapter):
    """Enumerate only the roles published on Sarvam AI's own careers index."""

    VERIFICATION_REVISION = "sarvam-ai.careers-astro-index.ashby-linked-details.1"

    async def _listing(self) -> tuple[dict, dict]:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_ENTRY:
            raise ValueError("Sarvam AI careers entry redirected away from its inspected URL")
        parsed = parse_careers_listing(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed, {"url": OFFICIAL_ENTRY, "final_url": str(response.url),
                        "status": response.status_code, "request_seconds": seconds,
                        "response_sha256": parsed["response_sha256"], "response_bytes": len(response.content)}

    async def _listing_component(self, component_url: str) -> dict:
        url = f"https://www.sarvam.ai{component_url}"
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError("Sarvam AI listing component moved away from its published URL")
        return assert_listing_component(response.text, url)

    async def _board(self) -> tuple[dict, dict]:
        started = time.perf_counter()
        response = await self.http.get(ASHBY_BOARD_API)
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.hostname != "api.ashbyhq.com" or parts.path != f"/posting-api/job-board/{ASHBY_BOARD}":
            raise ValueError("Sarvam AI linked Ashby board request left its company-scoped route")
        parsed = parse_ashby_board(response.json())
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed, {"url": ASHBY_BOARD_API, "final_url": str(response.url),
                        "status": response.status_code, "request_seconds": seconds,
                        "response_sha256": parsed["response_sha256"], "response_bytes": len(response.content)}

    async def _detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail_page(response.text, job.raw["listing"], job.raw["board"], job.url)
        parsed.update(status=response.status_code, request_seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "sarvam.careers.detail.jsonld_jobposting.description"
        job.description_source_url = job.url
        job.apply_url = parsed["apply_url"]
        job.date_posted = parsed["date_posted"]
        job.date_updated = job.raw["listing"]["updated_at"]
        job.locations = parsed["locations"]
        job.workplace_type = parsed["workplace_type"]
        job.employer = parsed["employer"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.raw["detail"] = parsed
        if parsed["role_attachments"]:
            job.warnings.append("role_attachment_requires_review")
        if parsed["rendered_differs_only_by_cloudflare_email_mask"]:
            job.warnings.append("rendered_body_email_masked_by_cloudflare")
        job.description_verified = False
        return job

    @staticmethod
    def _reconcile(listing: dict, board: dict) -> tuple[list[dict], list[str]]:
        issues = []
        if listing["job_ids"] != listing["props_job_ids"]:
            if set(listing["job_ids"]) != set(listing["props_job_ids"]):
                issues.append("sarvam_rendered_and_embedded_job_id_sets_differ")
        if set(listing["job_ids"]) != set(board["job_ids"]):
            issues.append("sarvam_careers_and_linked_board_membership_differ")
        if listing["count"] != board["count"]:
            issues.append("sarvam_careers_and_linked_board_count_differ")
        if listing["pagination_controls"]:
            issues.append("sarvam_listing_exposes_unreviewed_pagination_controls")
        rows = []
        for identity in listing["job_ids"]:
            row = listing["rows_by_id"].get(identity)
            board_row = board["jobs_by_id"].get(identity)
            if row is None or board_row is None:
                issues.append(f"sarvam_linked_job_missing_source_row:{identity}")
                continue
            checks = {
                "title": normalized_title(row["title"]) == normalized_title(board_row["title"]),
                "department": normalized_title(row["department"]) == normalized_title(board_row["department"]),
                "location": row["location"] == board_row["location"],
                "employment_type": row["employment_type"] == ASHBY_EMPLOYMENT[board_row["employmentType"]],
                "workplace": row["workplace"] == ASHBY_WORKPLACE[board_row["workplaceType"]],
                "apply_url": row["apply_url"] == board_row["applyUrl"],
                "posted_date": row["created_at"] == str(board_row["publishedAt"])[:10],
            }
            for field, matches in checks.items():
                if not matches:
                    issues.append(f"sarvam_listing_board_mismatch:{identity}:{field}")
            if all(checks.values()):
                rows.append(row)
        if len(rows) != listing["count"]:
            issues.append("sarvam_reconciled_rows_do_not_match_listing_count")
        return rows, issues

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": (
                "Sarvam AI's own /careers page ships all published roles in its Astro "
                "JobListingSection island and renders one card per role; filters run in the browser."
            ),
            "detail_method": (
                "Every exact /careers/jobs/<uuid> page is fetched and its JSON-LD JobPosting.description "
                "is retained; it is cross-checked against the same role on Sarvam's linked Ashby board."
            ),
            "request_scope": (
                "Only www.sarvam.ai/careers, its own detail pages, the careers listing component, and "
                "the company-linked board api.ashbyhq.com/posting-api/job-board/sarvam."
            ),
            "linked_board_api": ASHBY_BOARD_API,
        })
        try:
            listing, listing_request = await self._listing()
            component = await self._listing_component(listing["component_url"])
            board, board_request = await self._board()
        except Exception as exc:
            self.result.errors.append(f"Sarvam AI source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["source_collection_wall_seconds"] = self.result.metadata["source_collection_wall_seconds"]
            return self.result

        self.result.metadata["initial_listing_request"] = listing_request
        self.result.metadata["listing_component"] = component
        self.result.metadata["initial_linked_board_request"] = board_request
        self.result.metadata["initial_listing"] = {
            key: value for key, value in listing.items()
            if key not in {"rows", "rows_by_id", "rendered_rows"}
        }
        self.result.metadata["initial_linked_board"] = {
            key: value for key, value in board.items() if key not in {"rows", "jobs_by_id"}
        }
        for issue in listing["issues"] + board["issues"]:
            if issue not in self.result.errors:
                self.result.errors.append(issue)

        rows, issues = self._reconcile(listing, board)
        for issue in issues:
            if issue not in self.result.errors:
                self.result.errors.append(issue)
        self.result.expected_total = listing["count"]
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["source_collection_wall_seconds"] = self.result.metadata["source_collection_wall_seconds"]
        self.result.pages = 2

        jobs = []
        for row in rows:
            board_row = board["jobs_by_id"][row["id"]]
            job = self.job(
                {"listing": row, "board": board_row, "board_api": ASHBY_BOARD_API},
                title=normalized_title(row["title"]),
                url=DETAIL_URL_TEMPLATE.format(job_id=row["id"]),
                job_id=row["id"],
                requisition_id=None,
                apply_url=row["apply_url"],
                description="",
                method=None,
                description_url=None,
                locations=_locations(board_row),
                department=normalized_title(row["department"]),
                team=board_row.get("team"),
                employment_type=row["employment_label"],
                workplace_type=row["workplace_type"],
                date_posted=row["created_at"],
                date_posted_raw={"listing_created_at": row["created_at"],
                                 "ashby_published_at": board_row["publishedAt"]},
                date_updated=row["updated_at"],
                valid_through=None,
                salary=None,
                employer={"brand": BRAND, "legal_name": EXPECTED_LEGAL_EMPLOYER,
                          "legal_name_source": OFFICIAL_ENTRY},
                language="en",
            )
            self.add(job)
            jobs.append(job)

        self.result.metadata["listing_membership"] = {
            "initial_ids": listing["job_ids"],
            "props_ids": listing["props_job_ids"],
            "linked_board_ids": board["job_ids"],
            "initial_count": listing["count"],
            "linked_board_count": board["count"],
            "rendered_card_count": len(listing["rendered_rows"]),
            "rendered_heading_count": listing["total_heading"],
            "department_groups": listing["department_groups"],
            "filters": listing["filters"],
            "pagination_controls": listing["pagination_controls"],
            "search_or_filter_applied": False,
        }

        await self.hydrate(jobs, self._detail)
        detail_issues = list(self.result.errors)

        final_listing = final_board = None
        final_started = time.perf_counter()
        try:
            final_listing, final_listing_request = await self._listing()
            final_board, final_board_request = await self._board()
        except Exception as exc:
            self.result.errors.append(f"Sarvam AI final membership recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_membership_recheck_seconds"] = max(
            0.0, time.perf_counter() - final_started)

        reconciliation = {
            "initial_ids": listing["job_ids"],
            "initial_count": listing["count"],
            "linked_board_count": board["count"],
            "complete": False,
            "new_ids": [],
            "removed_ids": [],
            "changed_fields_by_id": {},
            "order_changed": False,
        }
        if final_listing is not None and final_board is not None:
            self.result.metadata["final_listing_request"] = final_listing_request
            self.result.metadata["final_linked_board_request"] = final_board_request
            final_ids = final_listing["job_ids"]
            initial_ids = listing["job_ids"]
            reconciliation.update({
                "final_ids": final_ids,
                "final_count": final_listing["count"],
                "final_linked_board_count": final_board["count"],
                "new_ids": sorted(set(final_ids) - set(initial_ids)),
                "removed_ids": sorted(set(initial_ids) - set(final_ids)),
                "order_changed": final_ids != initial_ids,
            })
            initial_by_id = {row["id"]: row for row in listing["rows"]}
            final_by_id = {row["id"]: row for row in final_listing["rows"]}
            for identity in initial_ids:
                if identity not in final_by_id:
                    continue
                changed = sorted(
                    field for field in initial_by_id[identity]
                    if initial_by_id[identity][field] != final_by_id[identity][field]
                )
                if changed:
                    reconciliation["changed_fields_by_id"][identity] = changed
            final_board_ids = set(final_board["job_ids"])
            if reconciliation["new_ids"] or reconciliation["removed_ids"]:
                self.result.errors.append("sarvam_listing_membership_changed_during_collection")
            if final_board_ids != set(final_listing["job_ids"]):
                self.result.errors.append("sarvam_final_careers_and_linked_board_membership_differ")
            if final_listing["count"] != final_board["count"]:
                self.result.errors.append("sarvam_final_careers_and_linked_board_count_differ")
            reconciliation["complete"] = (
                not reconciliation["new_ids"] and not reconciliation["removed_ids"]
                and not reconciliation["changed_fields_by_id"]
                and final_listing["count"] == listing["count"] == board["count"]
                and final_board_ids == set(initial_ids)
            )
        if any(job.description_unavailable for job in jobs):
            self.result.errors.append("sarvam_detail_description_unavailable")
        verified = sum(job.description_verified for job in jobs)
        self.result.listing_complete = bool(
            reconciliation["complete"] and jobs and len(jobs) == listing["count"]
            and verified == len(jobs) and not self.result.errors
        )
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["detail_failures"] = [
            {"id": job.job_id, "url": job.url,
             "reasons": [warning for warning in job.warnings if warning != "short_or_missing_description"]}
            for job in jobs if not job.description_verified
        ]
        if reconciliation["complete"] and jobs and all("detail" in job.raw for job in jobs):
            first, last = jobs[0], jobs[-1]
            self.result.metadata["representative_details"] = {
                job.job_id: {
                    "url": job.url,
                    "title": job.title,
                    "description_normalized_chars": job.raw["detail"]["description_normalized_chars"],
                    "description_fnv1a64": job.raw["detail"]["description_fnv1a64"],
                    "rendered_body_normalized_chars": job.raw["detail"]["rendered_body_normalized_chars"],
                    "rendered_body_fnv1a64": job.raw["detail"]["rendered_body_fnv1a64"],
                    "matches_linked_board_description": job.raw["detail"]["matches_linked_board_description"],
                }
                for job in (first, last)
            }
        if not detail_issues:
            self.result.warnings = [w for w in self.result.warnings if w != "sample_limit"]
        return self.result
