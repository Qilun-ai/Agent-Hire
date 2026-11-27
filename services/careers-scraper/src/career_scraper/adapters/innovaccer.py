"""Innovaccer's own Webflow careers listing and its exact Workable account details."""
from __future__ import annotations

import asyncio
from hashlib import sha256
from html import unescape
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://innovaccer.com/careers"
LISTING_URL = "https://innovaccer.com/careers/jobs"
WORKABLE_ACCOUNT = "innovaccer-analytics"
WORKABLE_ACCOUNT_NAME = "Innovaccer Analytics"
WORKABLE_ORIGIN = "https://apply.workable.com"
WORKABLE_WIDGET_API = (
    "https://apply.workable.com/api/v1/widget/accounts/innovaccer-analytics?details=true"
)
WORKABLE_BOARD_MD = "https://apply.workable.com/innovaccer-analytics/jobs.md"
WORKABLE_DETAIL_MD = (
    "https://apply.workable.com/innovaccer-analytics/jobs/view/{job_id}.md"
)
WORKABLE_JOB_URL = "https://apply.workable.com/j/{job_id}"
WORKABLE_CANONICAL_URL = "https://apply.workable.com/innovaccer-analytics/j/{job_id}"
WORKABLE_APPLY_URL = (
    "https://apply.workable.com/innovaccer-analytics/j/{job_id}/apply"
)
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
JOB_LINK = re.compile(r"^https://apply\.workable\.com/j/([0-9A-Z]{10})/?$")
SHORTCODE = re.compile(r"^[0-9A-Z]{10}$")
MARKDOWN_APPLY = re.compile(
    r"\[Apply at [^\]]+\]"
    r"\((https://apply\.workable\.com/innovaccer-analytics/j/([0-9A-Z]{10})/apply)\)"
)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
WORKPLACE_LABELS = {"on_site": "On-site", "remote": "Remote", "hybrid": "Hybrid"}
CARD_EMPLOYMENT_LABELS = {
    "full time": "Full-time",
    "full-time": "Full-time",
    "others": "Other",
    "other": "Other",
    "intern": "Intern",
    "internship": "Intern",
    "apprentice": "Apprentice",
    "apprenticeship": "Apprentice",
    "contract": "Contract",
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """Whitespace/punctuation normalisation used for rendered-body comparisons."""
    value = unescape(value or "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def body_tokens(value: str | None) -> list[str]:
    """Markdown-syntax-insensitive token sequence for a role body."""
    value = unescape(value or "").replace("\xa0", " ")
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>])", r"\1", value)
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", " ", value)
    value = re.sub(r"(?m)^\s*(?:[-*+]|\d+\.)\s+", " ", value)
    return re.findall(r"[a-z0-9]+", value.lower())


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _official_host(host: str) -> bool:
    return host in {"innovaccer.com", "www.innovaccer.com"}


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or not _official_host(parts.netloc)
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Innovaccer official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Careers" not in title:
        raise ValueError("Innovaccer official careers entry title changed")
    page_text = soup.get_text(" ", strip=True)
    if "Innovaccer" not in page_text:
        raise ValueError("Innovaccer official careers entry no longer identifies the company")

    jobs_links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        target_parts = urlsplit(target)
        if (_official_host(target_parts.netloc)
                and target_parts.path.rstrip("/") == "/careers/jobs"):
            if target not in jobs_links:
                jobs_links.append(target)
    if LISTING_URL not in jobs_links:
        raise ValueError("Innovaccer official careers entry no longer links to its jobs listing")
    return {
        "url": response_url,
        "title": title,
        "careers_jobs_url": LISTING_URL,
        "jobs_links": jobs_links,
        "official_host": parts.netloc,
    }


def parse_filter_groups(soup: BeautifulSoup) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for field in soup.select("div.text-field"):
        toggle = field.select_one(".textfield-dropdown")
        if toggle is None:
            continue
        label = normalized_title(toggle.get_text(" ", strip=True))
        if not label:
            continue
        options = []
        for checkbox in field.select('input[type="checkbox"][data-name]'):
            value = normalized_title(checkbox.get("data-name"))
            if value and value not in options:
                options.append(value)
        if options:
            groups.setdefault(label, []).extend(
                value for value in options if value not in groups.get(label, [])
            )
    return groups


def parse_jobs_listing(html: str, response_url: str = LISTING_URL) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or not _official_host(parts.netloc)
            or parts.path.rstrip("/") != "/careers/jobs"):
        raise ValueError("Innovaccer jobs listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Jobs" not in page_title:
        raise ValueError("Innovaccer jobs listing title changed")

    cards, issues, seen = [], [], set()
    for item in soup.select("div.w-dyn-item"):
        anchor = item.select_one('a[href*="apply.workable.com/j/"]')
        if anchor is None:
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        match = JOB_LINK.fullmatch(target)
        if match is None:
            issues.append(f"unexpected_company_job_link:{target}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"duplicate_company_job_link:{identity}")
            continue
        title_node = item.select_one(".job-title")
        workplace_node = item.select_one(".workplace-type")
        location_node = item.select_one(".job-location")
        department_node = item.select_one(".job-dept")
        worktype_node = item.select_one(".job-work-type")
        title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
        workplace = normalized_title(workplace_node.get_text(" ", strip=True)) if workplace_node else ""
        location = normalized_title(location_node.get_text(" ", strip=True)) if location_node else ""
        department = normalized_title(department_node.get_text(" ", strip=True)) if department_node else ""
        worktype = normalized_title(worktype_node.get_text(" ", strip=True)) if worktype_node else ""
        if not title or not workplace or not location or not department or not worktype:
            issues.append(f"company_job_card_fields_missing:{identity}")
            continue
        seen.add(identity)
        cards.append({
            "id": identity,
            "url": target,
            "title": title,
            "workplace": workplace,
            "location": location,
            "department": department,
            "worktype": worktype,
        })
    if not cards:
        issues.append("innovaccer_company_listing_has_no_exposed_job_cards")

    scripts = "\n".join(
        script.get_text() for script in soup.select("script") if script.get_text().strip()
    )
    count_match = re.search(r"SHOW_COUNT\s*=\s*(\d+)", scripts)
    show_more = soup.select_one("#show-more-jobs")
    return {
        "url": response_url,
        "page_title": page_title,
        "cards": cards,
        "job_ids": [card["id"] for card in cards],
        "count": len(cards),
        "show_more_control": bool(show_more),
        "initial_visible_count": int(count_match.group(1)) if count_match else None,
        "client_pagination": bool(show_more and count_match),
        "search_available": bool(soup.select_one("#search-input")),
        "filters": parse_filter_groups(soup),
        "pagination_controls": (["SHOW MORE (client-side, 10 cards per click)"] if show_more else []),
        "issues": issues,
    }


def _location_label(location: dict) -> str:
    parts = [
        normalized_title(location.get("city")),
        normalized_title(location.get("region")),
        normalized_title(location.get("country")),
    ]
    return ", ".join(part for part in parts if part)


def _location_key(location: dict) -> tuple:
    return (
        normalized_title(location.get("country")),
        normalized_title(location.get("region")),
        normalized_title(location.get("city")),
        bool(location.get("hidden")),
    )


def parse_workable_widget(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Innovaccer Workable widget response is not an object")
    if value.get("name") != WORKABLE_ACCOUNT_NAME:
        raise ValueError("Innovaccer Workable widget account name changed")
    raw_jobs = value.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("Innovaccer Workable widget response is missing its jobs array")

    issues, by_shortcode, duplicate_shortcodes = [], {}, {}
    invariant_fields = (
        "title", "department", "employment_type", "url", "shortlink",
        "application_url", "published_on", "created_at", "code", "description",
    )
    for row in raw_jobs:
        if not isinstance(row, dict):
            issues.append("workable_widget_row_not_object")
            continue
        identity = normalized_title(row.get("shortcode"))
        if not SHORTCODE.fullmatch(identity):
            issues.append("workable_widget_row_missing_or_invalid_shortcode")
            continue
        title = normalized_title(row.get("title"))
        description = row.get("description")
        if not title or not isinstance(description, str) or not description.strip():
            issues.append(f"workable_widget_row_missing_title_or_description:{identity}")
            continue
        if identity in by_shortcode:
            base = by_shortcode[identity]
            for field in invariant_fields:
                if row.get(field) != base["row"].get(field):
                    issues.append(f"workable_widget_duplicate_row_field_changed:{identity}:{field}")
            base["rows"].append(row)
            if identity not in duplicate_shortcodes:
                duplicate_shortcodes[identity] = [base["row"]]
            duplicate_shortcodes[identity].append(row)
        else:
            by_shortcode[identity] = {"row": row, "rows": [row]}
        entry = by_shortcode[identity]
        locations = entry.setdefault("locations", [])
        keys = {_location_key(location) for location in locations}
        for location in row.get("locations") or []:
            if isinstance(location, dict) and _location_key(location) not in keys:
                locations.append(dict(location))
                keys.add(_location_key(location))
        if not locations and any(row.get(field) for field in ("country", "city", "state")):
            location = {
                "country": row.get("country"),
                "region": row.get("state"),
                "city": row.get("city"),
                "hidden": False,
            }
            locations.append(location)

    for identity, entry in by_shortcode.items():
        labels = []
        for location in entry["locations"]:
            label = _location_label(location)
            if label and label not in labels:
                labels.append(label)
        entry["location_labels"] = labels
    if len(raw_jobs) != len(by_shortcode):
        # This account publishes one posting once per location. It is a location
        # expansion, not a second posting; retain the raw rows and report both.
        pass
    return {
        "account_name": value.get("name"),
        "raw_rows": len(raw_jobs),
        "unique_count": len(by_shortcode),
        "jobs": by_shortcode,
        "duplicate_shortcodes": duplicate_shortcodes,
        "issues": issues,
    }


def parse_workable_markdown(text: str, expected_job_id: str) -> dict:
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("Innovaccer Workable markdown is missing its title")
    title = normalized_title(lines[0][2:])
    if not title:
        raise ValueError("Innovaccer Workable markdown title is empty")
    header_line = next((line for line in lines[:12] if line.startswith("> ")), "")
    parts = [part.strip() for part in header_line[2:].split(" · ")]
    company = parts[0] if parts else None
    location = parts[1] if len(parts) > 1 else None
    employment = parts[2] if len(parts) > 2 else None
    if employment in {"", "—", "–", "-"}:
        employment = None
    posted_match = re.search(r"Posted\s+(\d{4}-\d{2}-\d{2})", header_line)
    posted = posted_match.group(1) if posted_match else None
    workplace_match = re.search(r"^\*\*Workplace:\*\*\s*(\S+)\s*$", text, re.M)
    department_match = re.search(r"^\*\*Department:\*\*\s*(.+?)\s*$", text, re.M)
    workplace = workplace_match.group(1) if workplace_match else None
    department = normalized_title(department_match.group(1)) if department_match else None

    start = text.find("## Description")
    if start < 0:
        raise ValueError("Innovaccer Workable markdown is missing its Description section")
    end = text.find("## Apply")
    body = text[start + len("## Description"):end if end >= 0 else len(text)].strip()
    if not body:
        raise ValueError("Innovaccer Workable markdown has an empty Description section")

    apply_match = MARKDOWN_APPLY.search(text)
    apply_url = apply_match.group(1) if apply_match else None
    apply_job_id = apply_match.group(2) if apply_match else None
    body_token_values = body_tokens(body)
    issues = []
    if company != WORKABLE_ACCOUNT_NAME:
        issues.append("markdown_company_name_changed")
    if apply_job_id != expected_job_id:
        issues.append("markdown_apply_id_mismatch")
    if not posted:
        issues.append("markdown_posted_date_missing")
    if not workplace:
        issues.append("markdown_workplace_missing")
    if not department:
        issues.append("markdown_department_missing")
    return {
        "title": title,
        "company": company,
        "location": location,
        "employment": employment,
        "posted": posted,
        "posted_raw": header_line[2:].strip() if header_line else None,
        "workplace": workplace,
        "department": department,
        "apply_url": apply_url,
        "apply_job_id": apply_job_id,
        "body_markdown": body,
        "body_chars": len(body),
        "body_token_count": len(body_token_values),
        "body_tokens_sha256": sha256(" ".join(body_token_values).encode()).hexdigest(),
        "body_tokens": body_token_values,
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-innovaccer-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-innovaccer-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
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
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _section_text(sections: list[dict]) -> list[dict]:
    return [
        section for section in sections
        if section["heading"] and section["text"]
    ]


def _responsibilities(sections: list[dict]) -> list[dict]:
    tokens = (
        "responsibilit", "what you will do", "what you'll do", "a day in the life",
        "your role", "key responsibilities",
    )
    return [section for section in _section_text(sections)
            if any(token in section["heading"].lower() for token in tokens)]


def _qualifications(sections: list[dict]) -> list[dict]:
    tokens = (
        "qualif", "requirement", "what you need", "what you'll need", "who you are",
        "required skills", "skills", "experience", "about you",
    )
    return [
        section for section in _section_text(sections)
        if "innovaccer" not in section["heading"].lower()
        and any(token in section["heading"].lower() for token in tokens)
    ]


def _description_links(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    return [
        {"text": normalized_title(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
        for anchor in soup.select("a[href]")
    ]


def _requisition_code(row: dict) -> str | None:
    code = normalized_title(row.get("code"))
    if re.fullmatch(r"HIR-[0-9A-Z]{2,}", code) and "*" not in code:
        return code
    return None


def _card_employment(card_value: str, detail_value: str | None, widget_value: str | None) -> str | None:
    key = normalized_title(card_value).lower()
    if key in CARD_EMPLOYMENT_LABELS:
        return CARD_EMPLOYMENT_LABELS[key]
    if detail_value:
        return detail_value
    if widget_value:
        return widget_value
    return None


def _workplace_label(detail_value: str | None, card_value: str) -> str | None:
    if detail_value in WORKPLACE_LABELS:
        return WORKPLACE_LABELS[detail_value]
    key = normalized_title(card_value).lower().replace("_", "-")
    if key in {"on-site", "onsite"}:
        return "On-site"
    if key in {"remote"}:
        return "Remote"
    if key in {"hybrid"}:
        return "Hybrid"
    return normalized_title(card_value) or None


class Innovaccer(Adapter):
    """Collect only the jobs published on Innovaccer's own Webflow careers listing."""

    VERIFICATION_REVISION = "innovaccer.webflow-workable.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"User-Agent": BROWSER_UA})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL, headers={"User-Agent": BROWSER_UA})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_jobs_listing(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _widget(self):
        started = time.perf_counter()
        response = await self.http.request(
            "GET",
            WORKABLE_WIDGET_API,
            headers={
                "Accept": "application/json",
                "Referer": f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/",
                "User-Agent": BROWSER_UA,
            },
        )
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if (parts.scheme != "https" or parts.netloc != "apply.workable.com"
                or parts.path != f"/api/v1/widget/accounts/{WORKABLE_ACCOUNT}"):
            raise ValueError("Innovaccer Workable widget redirected away from its company-scoped route")
        parsed = parse_workable_widget(response.json())
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            url=str(response.url),
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _detail_markdown(self, identity: str):
        url = WORKABLE_DETAIL_MD.format(job_id=identity)
        started = time.perf_counter()
        response = await self.http.get(
            url,
            headers={
                "Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.8",
                "User-Agent": BROWSER_UA,
            },
        )
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"Innovaccer Workable detail redirected away from its exact route: {identity}")
        parsed = parse_workable_markdown(response.text, identity)
        parsed.update(
            url=url,
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    def _widget_signature(self, entry: dict) -> dict:
        row = entry["row"]
        return {
            "title": normalized_title(row.get("title")),
            "department": normalized_title(row.get("department")),
            "employment_type": normalized_title(row.get("employment_type")),
            "url": row.get("url"),
            "shortlink": row.get("shortlink"),
            "application_url": row.get("application_url"),
            "published_on": row.get("published_on"),
            "created_at": row.get("created_at"),
            "code": row.get("code"),
            "description_sha256": sha256((row.get("description") or "").encode()).hexdigest(),
            "locations": entry.get("locations") or [],
        }

    def _listing_signature(self, listing: dict) -> list[dict]:
        return [
            {
                "id": card["id"],
                "title": card["title"],
                "workplace": card["workplace"],
                "location": card["location"],
                "department": card["department"],
                "worktype": card["worktype"],
            }
            for card in listing["cards"]
        ]

    def _fallback_detail(self, card: dict, widget_entry: dict | None) -> dict:
        """Keep the usable widget description when an exact detail route fails."""
        row = widget_entry["row"] if widget_entry else {}
        return {
            "title": card["title"],
            "company": WORKABLE_ACCOUNT_NAME,
            "location": card["location"],
            "employment": row.get("employment_type") or None,
            "posted": row.get("published_on"),
            "posted_raw": None,
            "workplace": None,
            "department": None,
            "apply_url": WORKABLE_APPLY_URL.format(job_id=card["id"]),
            "apply_job_id": card["id"],
            "body_markdown": "",
            "body_chars": 0,
            "body_token_count": 0,
            "body_tokens_sha256": None,
            "body_tokens": [],
            "url": WORKABLE_DETAIL_MD.format(job_id=card["id"]),
            "status": None,
            "response_sha256": None,
            "response_bytes": 0,
            "seconds": 0.0,
            "issues": [],
        }

    def _make_job(self, card: dict, widget_entry: dict | None, detail: dict):
        row = widget_entry["row"] if widget_entry else {}
        description_html = row.get("description") or ""
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibilities = _responsibilities(sections)
        qualifications = _qualifications(sections)
        links = _description_links(description_html)
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link.get("url") or "")]
        locations = list(widget_entry.get("location_labels") or []) if widget_entry else []
        if not locations:
            locations = [card["location"]]
        detail_workplace = detail.get("workplace")
        workplace_type = _workplace_label(detail_workplace, card["workplace"])
        employment_type = _card_employment(
            card["worktype"],
            detail.get("employment"),
            row.get("employment_type"),
        )
        posted = detail.get("posted") or row.get("published_on")
        widget_tokens = body_tokens(text_html(description_html)) if description_html else []
        detail_tokens = detail.get("body_tokens") or []
        token_match = bool(widget_entry) and detail_tokens == widget_tokens
        detail_department = detail.get("department")
        raw = {
            "listing_card": dict(card),
            "workable": dict(row) if row else None,
            "workable_rows": [dict(item) for item in (widget_entry or {}).get("rows", [])],
            "workable_locations": [dict(item) for item in (widget_entry or {}).get("locations", [])],
            "markdown_header": {
                "title": detail.get("title"),
                "company": detail.get("company"),
                "location": detail.get("location"),
                "employment": detail.get("employment"),
                "posted": detail.get("posted"),
                "posted_raw": detail.get("posted_raw"),
                "workplace": detail.get("workplace"),
                "department": detail.get("department"),
                "apply_url": detail.get("apply_url"),
                "apply_job_id": detail.get("apply_job_id"),
            },
            "markdown": {
                "url": detail.get("url"),
                "status": detail.get("status"),
                "response_sha256": detail.get("response_sha256"),
                "response_bytes": detail.get("response_bytes"),
                "request_seconds": detail.get("seconds"),
                "body_sha256": sha256((detail.get("body_markdown") or "").encode()).hexdigest(),
                "body_chars": detail.get("body_chars"),
                "body_token_count": detail.get("body_token_count"),
                "body_tokens_sha256": detail.get("body_tokens_sha256"),
                "body_markdown": detail.get("body_markdown"),
            },
            "description": {
                "source_field": "Workable widget jobs[].description",
                "description_html_sha256": sha256(description_html.encode()).hexdigest(),
                "normalized_chars": len(normalized_body_text(description_html)),
                "normalized_fnv1a64": fnv1a64(normalized_body_text(description_html)),
                "markdown_token_sequence_matches": token_match,
                "sections": sections,
                "links": links,
                "compliance_attachment_links": attachments,
                "role_attachments": [],
                "salary_field_present_in_source": False,
                "salary_mentions_remain_in_description": True,
            },
            "application_route": {
                "company_listing_link": card["url"],
                "workable_canonical_url": WORKABLE_CANONICAL_URL.format(job_id=card["id"]),
                "exact_detail_markdown_url": detail.get("url"),
                "exact_apply_url": detail.get("apply_url") or WORKABLE_APPLY_URL.format(job_id=card["id"]),
                "apply_form_route": WORKABLE_APPLY_URL.format(job_id=card["id"]),
            },
            "dates": {
                "posted": posted,
                "posted_markdown": detail.get("posted"),
                "published_on": row.get("published_on"),
                "created_at": row.get("created_at"),
                "created_at_used_as_posting_date": False,
                "date_updated": None,
                "valid_through": None,
            },
            "department_card_parent": card["department"],
            "department_workable_parent": row.get("department"),
            "department_markdown_subdepartment": detail_department,
            "employment_type_card_raw": card["worktype"],
            "employment_type_workable_raw": row.get("employment_type"),
            "employment_type_markdown_raw": detail.get("employment"),
            "workplace_card_raw": card["workplace"],
            "workplace_markdown_raw": detail_workplace,
            "token_match": token_match,
        }
        job = self.job(
            raw,
            title=card["title"],
            url=card["url"],
            job_id=card["id"],
            requisition_id=_requisition_code(row),
            apply_url=detail.get("apply_url") or WORKABLE_APPLY_URL.format(job_id=card["id"]),
            description=description_html,
            method="innovaccer.workable.widget_description+exact_markdown_detail",
            description_url=detail.get("url") or WORKABLE_DETAIL_MD.format(job_id=card["id"]),
            locations=locations,
            department=card["department"],
            team=detail_department if detail_department and detail_department != card["department"] else None,
            employment_type=employment_type,
            workplace_type=workplace_type,
            date_posted=posted,
            date_posted_raw={
                "markdown": detail.get("posted_raw"),
                "published_on": row.get("published_on"),
                "created_at": row.get("created_at"),
            },
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=qualifications or None,
            responsibilities=responsibilities or None,
            experience=normalized_title(row.get("experience")) or None,
            education=normalized_title(row.get("education")) or None,
            employer=WORKABLE_ACCOUNT_NAME,
            language="en",
        )
        if not description_html and detail.get("body_markdown"):
            job.description_text = detail["body_markdown"]
        else:
            job.description_text = description_text
        return job, token_match

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "workable_account": WORKABLE_ACCOUNT,
            "workable_account_name": WORKABLE_ACCOUNT_NAME,
            "workable_board_markdown": WORKABLE_BOARD_MD,
            "listing_authority": "Innovaccer's own Webflow careers listing; all 95 cards are in the initial HTML.",
            "membership_pagination": "Client-side SHOW MORE only; 10 cards initially, 10 more per click, all 95 already in the DOM. No listing request is made by the pagination control.",
            "detail_method": "Exact company-linked Workable shortcode; full description from the public account-scoped widget, independently token-verified against jobs/view/<shortcode>.md.",
            "request_scope": "Only innovaccer.com/careers, innovaccer.com/careers/jobs, and the Innovaccer Analytics Workable account exact IDs; no other company board.",
        })
        try:
            official_started = time.perf_counter()
            official = await self._official_entry()
            official_seconds = max(0.0, time.perf_counter() - official_started)
            listing_started = time.perf_counter()
            listing = await self._listing()
            listing_seconds = max(0.0, time.perf_counter() - listing_started)
            widget_started = time.perf_counter()
            widget = await self._widget()
            widget_seconds = max(0.0, time.perf_counter() - widget_started)
        except Exception as exc:
            self.result.errors.append(f"Innovaccer source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        source_wall = max(0.0, time.perf_counter() - source_started)
        self.result.timings.update({
            "official_entry_seconds": official_seconds,
            "listing_request_seconds": listing_seconds,
            "workable_widget_request_seconds": widget_seconds,
            "source_collection_wall_seconds": source_wall,
        })
        self.result.expected_total = listing["count"]
        self.result.pages = 1
        self.result.metadata["initial_official_entry"] = official
        self.result.metadata["initial_listing"] = {k: v for k, v in listing.items() if k != "cards"}
        self.result.metadata["initial_listing_cards"] = listing["cards"]
        self.result.metadata["initial_workable_widget"] = {
            k: v for k, v in widget.items() if k not in {"jobs", "duplicate_shortcodes"}
        }
        self.result.metadata["workable_duplicate_shortcodes"] = {
            key: len(rows) for key, rows in widget["duplicate_shortcodes"].items()
        }
        self.result.errors.extend(listing["issues"])
        self.result.errors.extend(widget["issues"])

        card_ids = listing["job_ids"]
        widget_ids = list(widget["jobs"].keys())
        missing_in_widget = [identity for identity in card_ids if identity not in widget["jobs"]]
        extra_in_widget = [identity for identity in widget_ids if identity not in set(card_ids)]
        if missing_in_widget:
            self.result.errors.append(
                "innovaccer_company_cards_missing_workable_rows:" + ",".join(missing_in_widget)
            )
        if extra_in_widget:
            self.result.errors.append(
                "innovaccer_workable_rows_not_on_company_listing:" + ",".join(extra_in_widget)
            )
        if listing["count"] != widget["unique_count"]:
            self.result.errors.append("innovaccer_company_and_workable_membership_counts_differ")

        detail_started = time.perf_counter()
        detail_sem = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        detail_request_seconds = 0.0
        detail_parse_seconds = 0.0

        async def one_detail(card):
            nonlocal detail_request_seconds, detail_parse_seconds
            async with detail_sem:
                parse_started = time.perf_counter()
                try:
                    detail = await self._detail_markdown(card["id"])
                    detail_request_seconds += detail.get("seconds", 0.0)
                    detail_parse_seconds += max(0.0, time.perf_counter() - parse_started - detail.get("seconds", 0.0))
                    return card, detail, None
                except Exception as exc:
                    detail_parse_seconds += max(0.0, time.perf_counter() - parse_started)
                    return card, None, f"{type(exc).__name__}: {exc}"

        detail_results = await asyncio.gather(*(one_detail(card) for card in listing["cards"]))
        detail_wall = max(0.0, time.perf_counter() - detail_started)

        jobs, detail_failures, body_mismatches, header_mismatches = [], [], [], []
        for card, detail, error in detail_results:
            widget_entry = widget["jobs"].get(card["id"])
            detail_failed = error is not None or detail is None
            if detail_failed:
                detail_failures.append({"id": card["id"], "url": card["url"], "reason": error})
                self.result.errors.append(f"innovaccer_detail_fetch_failed:{card['id']}:{error}")
                detail = self._fallback_detail(card, widget_entry)
            if widget_entry is None:
                self.result.errors.append(f"innovaccer_detail_without_workable_row:{card['id']}")
            if normalized_title(detail["title"]) != normalized_title(card["title"]):
                header_mismatches.append(card["id"])
                self.result.errors.append(f"innovaccer_detail_card_title_mismatch:{card['id']}")
            if widget_entry and normalized_title(detail["title"]) != normalized_title(widget_entry["row"].get("title")):
                header_mismatches.append(card["id"])
                self.result.errors.append(f"innovaccer_detail_workable_title_mismatch:{card['id']}")
            if detail.get("issues"):
                self.result.errors.extend(
                    f"innovaccer_markdown:{card['id']}:{issue}" for issue in detail["issues"]
                )
            job, token_match = self._make_job(card, widget_entry, detail)
            if detail_failed:
                job.description_unavailable = True
                job.warnings.append("detail_fetch_failed")
            elif not token_match:
                body_mismatches.append(card["id"])
                self.result.errors.append(f"innovaccer_detail_body_token_mismatch:{card['id']}")
                job.description_unavailable = True
                job.warnings.append("detail_body_mismatch")
            if widget_entry:
                published_on = widget_entry["row"].get("published_on")
                if detail.get("posted") and published_on and detail["posted"] != published_on:
                    self.result.errors.append(f"innovaccer_detail_posted_date_mismatch:{card['id']}")
                detail_workplace = _workplace_label(detail.get("workplace"), card["workplace"])
                if detail_workplace and normalized_title(detail_workplace).lower() != normalized_title(card["workplace"]).lower():
                    self.result.errors.append(f"innovaccer_detail_workplace_mismatch:{card['id']}")
            self.add(job)
            jobs.append(job)

        detail_failure_ids = {item["id"] for item in detail_failures}
        for identity in card_ids:
            if identity not in {job.job_id for job in jobs} and identity not in detail_failure_ids:
                self.result.errors.append(f"innovaccer_retained_membership_gap:{identity}")

        if detail_failures:
            self.result.metadata["detail_failures"] = detail_failures
        if body_mismatches:
            self.result.metadata["body_mismatches"] = body_mismatches
        if header_mismatches:
            self.result.metadata["header_mismatches"] = sorted(set(header_mismatches))
        self.result.timings["markdown_detail_request_seconds_sum"] = detail_request_seconds
        self.result.timings["markdown_detail_parse_seconds_sum"] = detail_parse_seconds
        self.result.timings["detail_phase_seconds"] = detail_wall
        self.result.metadata["detail_capture"] = {
            "requested": len(listing["cards"]),
            "markdown_succeeded": len(listing["cards"]) - len(detail_failures),
            "markdown_failed": len(detail_failures),
            "jobs_retained": len(jobs),
            "body_token_matches": len(jobs) - len(detail_failures) - len(body_mismatches),
            "body_token_mismatches": len(body_mismatches),
            "concurrency": max(1, self.options.detail_concurrency),
        }

        final_started = time.perf_counter()
        final = None
        try:
            final_official = await self._official_entry()
            final_listing = await self._listing()
            final_widget = await self._widget()
            final = (final_official, final_listing, final_widget)
        except Exception as exc:
            self.result.errors.append(f"Innovaccer final source recheck failed: {type(exc).__name__}: {exc}")
            final_listing = None
            final_widget = None
            final_official = None
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_recheck_wall_seconds"] = final_seconds

        final_matches = False
        if final is not None:
            final_ids = final_listing["job_ids"]
            initial_signature = self._listing_signature(listing)
            final_signature = self._listing_signature(final_listing)
            initial_widget_signatures = {
                key: self._widget_signature(entry) for key, entry in widget["jobs"].items()
            }
            final_widget_signatures = {
                key: self._widget_signature(entry) for key, entry in final_widget["jobs"].items()
            }
            changed_cards = [
                card["id"] for card, other in zip(listing["cards"], final_listing["cards"])
                if card != other
            ]
            changed_widget_fields = {}
            for identity in sorted(set(initial_widget_signatures) | set(final_widget_signatures)):
                before = initial_widget_signatures.get(identity)
                after = final_widget_signatures.get(identity)
                if before != after:
                    changed_widget_fields[identity] = sorted(
                        key for key in set(before or {}) | set(after or {})
                        if (before or {}).get(key) != (after or {}).get(key)
                    )
            same_card_ids = initial_signature == final_signature
            same_widget_ids = set(widget["jobs"]) == set(final_widget["jobs"])
            same_widget_fields = not changed_widget_fields
            same_official = final_official["careers_jobs_url"] == LISTING_URL
            final_matches = (
                same_card_ids and same_widget_ids and same_widget_fields and same_official
                and final_listing["count"] == listing["count"]
                and final_widget["unique_count"] == widget["unique_count"]
                and not final_listing["issues"] and not final_widget["issues"]
            )
            self.result.metadata["membership_reconciliation"] = {
                "initial_card_ids": card_ids,
                "final_card_ids": final_ids,
                "initial_widget_ids": widget_ids,
                "final_widget_ids": list(final_widget["jobs"].keys()),
                "initial_card_count": listing["count"],
                "final_card_count": final_listing["count"],
                "initial_widget_raw_rows": widget["raw_rows"],
                "final_widget_raw_rows": final_widget["raw_rows"],
                "initial_widget_unique_count": widget["unique_count"],
                "final_widget_unique_count": final_widget["unique_count"],
                "duplicate_shortcodes": {
                    key: len(rows) for key, rows in widget["duplicate_shortcodes"].items()
                },
                "company_and_workable_sets_match": set(card_ids) == set(widget_ids),
                "same_card_order_and_fields": same_card_ids,
                "same_widget_ids": same_widget_ids,
                "same_widget_fields": same_widget_fields,
                "changed_cards": changed_cards,
                "changed_widget_fields": changed_widget_fields,
                "official_jobs_link_still_exact": same_official,
                "complete": final_matches,
            }
            if not same_card_ids:
                self.result.errors.append("innovaccer_company_listing_changed_during_collection")
            if not same_widget_ids or not same_widget_fields:
                self.result.errors.append("innovaccer_workable_listing_changed_during_collection")
            if not same_official:
                self.result.errors.append("innovaccer_official_jobs_link_changed_during_collection")
            self.result.errors.extend(final_listing["issues"])
            self.result.errors.extend(final_widget["issues"])
        else:
            self.result.metadata["membership_reconciliation"] = {"complete": False}

        retained_ids = [job.job_id for job in jobs]
        if retained_ids != card_ids:
            self.result.errors.append("innovaccer_retained_jobs_do_not_match_company_card_order")
        verified = sum(job.description_verified for job in jobs)
        self.result.listing_complete = bool(
            final_matches
            and len(jobs) == listing["count"]
            and verified == len(jobs)
            and not self.result.errors
        )
        self.result.metadata["job_ids"] = retained_ids
        self.result.metadata["company_card_count"] = listing["count"]
        self.result.metadata["workable_unique_count"] = widget["unique_count"]
        self.result.metadata["workable_raw_row_count"] = widget["raw_rows"]
        self.result.metadata["verified_description_count"] = verified
        self.result.metadata["current_membership_reconciled"] = final_matches
        self.result.timings["source_collection_wall_seconds"] = source_wall
        return self.result
