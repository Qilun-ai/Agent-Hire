"""Richpanel's own Webflow careers listing and its exact Workable account details.

Observed journey (2026-11-08 UTC):

* ``https://www.richpanel.com/careers`` is the official, self-hosted Webflow
  careers page. Heading "Current Openings" publishes three job cards
  (``div.collection-list-6 > div.w-dyn-item``); each card links to
  ``/careers/<slug>``. There is no pagination widget and ``?page=2`` returns the
  identical page, so the three cards are the complete company listing.
* Each company detail page carries the role's rich-text sections, its Location /
  Department / Type labels and an ``Apply here`` link to Richpanel's own
  Workable account ``richpanel-1`` (``.../j/<10-char shortcode>/apply``).
* That account is company-scoped: the public widget payload reports
  ``name = "Richpanel"``, ``llms.txt`` states "12 current openings" and
  ``jobs.md`` tabulates the same twelve shortcodes. These are the only
  Workable identifiers used; no guessed slug, aggregator or parent board.
* The company page and the account disagree in two reviewed ways: the
  ``/careers/lead-backend-engineer`` body actually contains a Head of Learning
  & Development description (the exact Workable posting ``91FA1456FC`` holds the
  correct Lead Backend Engineer text), and the ``/careers/customer-success-
  manager-b2b-saas`` apply link (``B6895EB6F5``) is no longer an open Workable
  posting. Both are recorded, never silently replaced.

Membership is therefore the union of the three company-published cards and the
twelve open roles of the company's own Workable account: thirteen distinct
postings. Descriptions come from the company detail page when its body matches
the posting, and from the exact company-linked Workable posting otherwise.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.richpanel.com/careers"
LISTING_URL = OFFICIAL_ENTRY
CAREERS_HOSTS = {"richpanel.com", "www.richpanel.com"}
WORKABLE_ACCOUNT = "richpanel-1"
WORKABLE_ACCOUNT_NAME = "Richpanel"
WORKABLE_ORIGIN = "https://apply.workable.com"
WORKABLE_WIDGET_API = (
    f"{WORKABLE_ORIGIN}/api/v1/widget/accounts/{WORKABLE_ACCOUNT}?details=true"
)
WORKABLE_BOARD_MD = f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/jobs.md"
WORKABLE_LLMS = f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/llms.txt"
WORKABLE_DETAIL_MD = (
    f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/jobs/view/{{job_id}}.md"
)
WORKABLE_JOB_URL = f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/j/{{job_id}}"
WORKABLE_APPLY_URL = f"{WORKABLE_ORIGIN}/{WORKABLE_ACCOUNT}/j/{{job_id}}/apply"
WORKABLE_COMPANY_URL = "https://richpanel.com"

JOB_LINK = re.compile(
    rf"^https://apply\.workable\.com/{WORKABLE_ACCOUNT}/j/([0-9A-Z]{{10}})/apply/?$"
)
SHORTCODE = re.compile(r"^[0-9A-Z]{10}$")
CARD_PATH = re.compile(r"^/careers/([a-z0-9][a-z0-9-]*)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SALARY = re.compile(r"^(?P<currency>[A-Z]{3})\s*(?P<min>[\d,]+)\s*[–-]\s*(?P<max>[\d,]+)$")
HIDDEN_CLASSES = {"hide", "hide-static", "w-condition-invisible"}
WRAPPER_HEADINGS = {"## description", "## requirements", "## benefits"}
BODY_SIMILARITY_THRESHOLD = 0.60
WORKPLACE_LABELS = {"on_site": "On-site", "remote": "Remote", "hybrid": "Hybrid"}


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def body_tokens(value: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalized_text(value).lower())


def token_set(value: str | None) -> set[str]:
    return set(body_tokens(value))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _response_sha(response) -> str:
    return sha256(response.content or b"").hexdigest()


def _is_hidden(node: Tag | None) -> bool:
    """True when the node or an ancestor is display:none in the page CSS."""
    current = node
    while current is not None and getattr(current, "get", None):
        classes = set(current.get("class") or [])
        if classes & HIDDEN_CLASSES:
            return True
        style = (current.get("style") or "").replace(" ", "").lower()
        if "display:none" in style:
            return True
        current = current.parent
    return False


def _label_map(soup: BeautifulSoup, selector: str) -> dict:
    labels = {}
    for card in soup.select(selector):
        label = card.select_one(".job-txt-depart")
        value = card.select_one(".job-txt-depart-head")
        if label is None or value is None:
            continue
        name = normalized_text(label.get_text(" ", strip=True)).lower()
        labels[name] = {
            "value": normalized_text(value.get_text(" ", strip=True)),
            "hidden": _is_hidden(card),
        }
    return labels


def _visible_rich_text(soup: BeautifulSoup) -> list[Tag]:
    blocks = []
    for node in soup.select("div.job-rte"):
        classes = set(node.get("class") or [])
        if classes & HIDDEN_CLASSES or _is_hidden(node):
            continue
        if not normalized_text(node.get_text(" ", strip=True)):
            continue
        blocks.append(node)
    return blocks


def description_sections(html: str) -> list[dict]:
    """Split a description into heading-delimited sections (heading may be empty)."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    root = soup.body or soup
    sections: list[dict] = []
    heading = ""
    buffer: list[str] = []

    def flush():
        text = normalized_text(" ".join(buffer))
        if text or heading:
            sections.append({"heading": heading, "text": text, "tokens": len(body_tokens(text))})

    def walk(container):
        nonlocal heading, buffer
        for child in container.children:
            if isinstance(child, Tag):
                if child.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    flush()
                    buffer = []
                    heading = normalized_text(child.get_text(" ", strip=True))
                    continue
                if child.name == "p" and len(child.find_all(True)) == 1 and child.find("strong"):
                    # Workable's "<p><strong>Requirements</strong></p>" separators.
                    strong = child.find("strong")
                    if normalized_text(strong.get_text(" ", strip=True)) == normalized_text(child.get_text(" ", strip=True)):
                        flush()
                        buffer = []
                        heading = normalized_text(strong.get_text(" ", strip=True))
                        continue
                walk(child)
            elif isinstance(child, str):
                if child.strip():
                    buffer.append(child)

    walk(root)
    flush()
    return [item for item in sections if item["text"]]


def _section_field(sections: list[dict], pattern: str) -> dict | None:
    matches = [
        {"heading": item["heading"], "text": item["text"]}
        for item in sections
        if re.search(pattern, item["heading"], re.I) and len(item["text"]) >= 20
    ]
    if not matches:
        return None
    return {
        "headings": [item["heading"] for item in matches],
        "text": "\n\n".join(item["text"] for item in matches),
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc not in CAREERS_HOSTS
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Richpanel official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "richpanel" not in title.lower() or "career" not in title.lower():
        raise ValueError("Richpanel official careers page title/identity changed")
    headings = [normalized_text(node.get_text(" ", strip=True)) for node in soup.select("h1,h2")]
    if "Current Openings" not in headings:
        raise ValueError("Richpanel official careers page no longer publishes 'Current Openings'")

    cards, issues = [], []
    seen: set[str] = set()
    for item in soup.select("div.collection-list-6.w-dyn-items > div.w-dyn-item"):
        anchor = item.select_one("a.job-wrap[href]")
        title_node = item.select_one(".job-txt-title")
        if anchor is None or title_node is None:
            issues.append("job_card_missing_link_or_title")
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        target_parts = urlsplit(target)
        match = CARD_PATH.fullmatch(target_parts.path)
        if (target_parts.scheme != "https" or target_parts.netloc not in CAREERS_HOSTS
                or match is None or target_parts.query or target_parts.fragment):
            issues.append(f"job_card_unexpected_link:{target}")
            continue
        slug = match.group(1)
        if slug in seen:
            issues.append(f"duplicate_job_card:{slug}")
            continue
        seen.add(slug)
        labels = {}
        for block in item.select("div.job-type1"):
            label_node = block.select_one(".job-txt-depart")
            value_node = block.select_one(".job-txt-depart-head")
            if label_node is None or value_node is None:
                issues.append(f"job_card_label_incomplete:{slug}")
                continue
            labels[normalized_text(label_node.get_text(" ", strip=True)).lower()] = {
                "value": normalized_text(value_node.get_text(" ", strip=True)),
                "hidden": _is_hidden(block),
            }
        cards.append({
            "slug": slug,
            "url": target,
            "title": normalized_text(title_node.get_text(" ", strip=True)),
            "labels": labels,
            "location": labels.get("location", {}).get("value"),
            "employment_type": labels.get("type", {}).get("value"),
            "department": labels.get("department", {}).get("value"),
            "card_text": normalized_text(item.get_text(" ", strip=True)),
        })

    if not cards:
        raise ValueError("Richpanel official careers listing exposes no job cards")
    pagination = soup.select(".w-pagination-wrapper, .w-pagination-next, a[href*='?page=']")
    return {
        "url": response_url,
        "title": title,
        "heading": "Current Openings",
        "cards": cards,
        "card_count": len(cards),
        "pagination_present": bool(pagination),
        "load_more_present": any(
            "load more" in normalized_text(node.get_text(" ", strip=True)).lower()
            for node in soup.select("a,button")
        ),
        "issues": issues,
    }


def parse_site_detail(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    match = CARD_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc not in CAREERS_HOSTS or match is None:
        raise ValueError("Richpanel job detail response came from an unexpected URL")
    slug = match.group(1)
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("h1.h2-24-job") or soup.select_one("h1")
    title = normalized_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        raise ValueError(f"Richpanel job detail page has no heading: {slug}")

    labels = _label_map(soup, "div.job_label-card")
    # "Apply here" jumps to the page's own form anchor; the real posting link is
    # the "Apply now" button (and, on some pages, a direct Workable anchor).
    apply_urls, embedded_form_anchors = [], []
    for anchor in soup.select("a.form-submit-button[href], a.apply-text-link[href], a[href*='apply.workable.com']"):
        target = urljoin(response_url, anchor.get("href", ""))
        if target.startswith("#") or target == response_url:
            continue
        if target.startswith(f"{response_url}#"):
            embedded_form_anchors.append(anchor.get("href"))
            continue
        if JOB_LINK.fullmatch(target) and target not in apply_urls:
            apply_urls.append(target)
    workable_ids = []
    for url in apply_urls:
        link = JOB_LINK.fullmatch(url)
        if link and link.group(1) not in workable_ids:
            workable_ids.append(link.group(1))

    blocks = _visible_rich_text(soup)
    body_html = "\n".join(str(node) for node in blocks)
    body_text = normalized_text(" ".join(node.get_text(" ", strip=True) for node in blocks))

    posted_node = soup.select_one("div.job_description div.job-description_heading")
    posted_text = normalized_text(posted_node.get_text(" ", strip=True)) if posted_node else ""
    posted_date = None
    for time_node in soup.select("time[datetime]"):
        if not _is_hidden(time_node):
            posted_date = time_node.get("datetime")
            break

    salary_label = labels.get("salary", {})
    salary = None if salary_label.get("hidden") else (salary_label.get("value") or None)
    return {
        "slug": slug,
        "url": response_url,
        "title": title,
        "labels": labels,
        "location": labels.get("location", {}).get("value"),
        "employment_type": labels.get("type", {}).get("value"),
        "department": labels.get("department", {}).get("value"),
        "salary": salary,
        "apply_urls": apply_urls,
        "embedded_form_anchors": embedded_form_anchors,
        "workable_ids": workable_ids,
        "workable_id": workable_ids[0] if workable_ids else None,
        "body_html": body_html,
        "body_html_sha256": sha256(body_html.encode()).hexdigest() if body_html else None,
        "body_text": body_text,
        "body_chars": len(body_text),
        "body_tokens": len(body_tokens(body_text)),
        "body_sha256": sha256(body_text.encode()).hexdigest() if body_text else None,
        "description_blocks": len(blocks),
        "posted_placeholder": {
            "present": bool(posted_node),
            "hidden": _is_hidden(posted_node) if posted_node else None,
            "text": posted_text,
            "lorem_ipsum": "lorem ipsum" in posted_text.lower(),
        },
        "published_date": posted_date,
        "placeholder_description": "lorem ipsum" in body_text.lower(),
    }


def parse_workable_widget(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Richpanel Workable widget payload is not an object")
    name = normalized_text(payload.get("name"))
    if name != WORKABLE_ACCOUNT_NAME:
        raise ValueError(f"Richpanel Workable account name changed: {name!r}")
    rows = payload.get("jobs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Richpanel Workable account publishes no job rows")
    parsed, issues, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("widget_row_not_object")
            continue
        code = str(row.get("shortcode") or "")
        if not SHORTCODE.fullmatch(code):
            issues.append(f"widget_row_bad_shortcode:{code!r}")
            continue
        if code in seen:
            issues.append(f"widget_row_duplicate_shortcode:{code}")
            continue
        seen.add(code)
        title = normalized_text(row.get("title"))
        description = row.get("description") or ""
        if not title or not normalized_text(text_html(description)):
            issues.append(f"widget_row_missing_title_or_description:{code}")
            continue
        published = row.get("published_on")
        if published is not None and not DATE.fullmatch(str(published)):
            issues.append(f"widget_row_bad_published_on:{code}")
            published = None
        parsed.append({
            "shortcode": code,
            "title": title,
            "code": row.get("code"),
            "application_url": row.get("application_url"),
            "employment_type": row.get("employment_type"),
            "department": row.get("department"),
            "published_on": published,
            "created_at": row.get("created_at"),
            "city": row.get("city"),
            "state": row.get("state"),
            "country": row.get("country"),
            "locations": row.get("locations") or [],
            "experience": row.get("experience"),
            "education": row.get("education"),
            "function": row.get("function"),
            "industry": row.get("industry"),
            "telecommuting": row.get("telecommuting"),
            "url": row.get("url"),
            "shortlink": row.get("shortlink"),
            "description_html": description,
            "description_text": text_html(description),
        })
    if not parsed:
        raise ValueError("Richpanel Workable widget payload exposes no usable job rows")
    return {
        "account_name": name,
        "account_description_html": payload.get("description") or "",
        "rows": parsed,
        "count": len(parsed),
        "issues": issues,
    }


def parse_workable_board_md(text: str) -> dict:
    lines = (text or "").splitlines()
    if not lines or not lines[0].startswith("# Richpanel"):
        raise ValueError("Richpanel Workable board markdown identity changed")
    rows, issues = [], []
    columns = None
    for line in lines:
        stripped = line.strip()
        if columns is None:
            if stripped.lower().startswith("| title |"):
                columns = [cell.strip().lower() for cell in stripped.strip("|").split("|")]
            continue
        if not stripped.startswith("|"):
            continue
        if set(stripped) <= {"|", "-", ":", " "}:
            continue
        cells = [cell.strip() for cell in re.split(r"\s+\|\s+", stripped.strip("|"))]
        if len(cells) > len(columns):
            # A job title may itself contain " | "; only the leading title cell is free text.
            extra = len(cells) - len(columns)
            cells = [" | ".join(cells[:extra + 1])] + cells[extra + 1:]
        if len(cells) != len(columns):
            issues.append(f"board_row_column_mismatch:{stripped[:60]}")
            continue
        row = dict(zip(columns, cells))
        link = re.search(r"/jobs/view/([0-9A-Z]{10})\.md", row.get("details", ""))
        salary = row.get("salary") or ""
        salary = None if salary in ("", "—", "-") else salary
        posted = row.get("posted") or ""
        posted = posted if DATE.fullmatch(posted) else None
        rows.append({
            "job_id": link.group(1) if link else None,
            "title": row.get("title"),
            "department": None if row.get("department") in ("", "—") else row.get("department"),
            "location": row.get("location"),
            "employment_type": row.get("type"),
            "salary": salary,
            "posted": posted,
            "details_url": row.get("details"),
        })
    if columns is None:
        raise ValueError("Richpanel Workable board markdown has no job table")
    if not rows:
        raise ValueError("Richpanel Workable board markdown lists no jobs")
    updated = next(
        (line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("> last updated:")),
        None,
    )
    return {"title": lines[0], "board_updated": updated, "rows": rows, "count": len(rows), "issues": issues}


def parse_workable_llms(text: str) -> dict:
    lines = (text or "").splitlines()
    if not lines or not lines[0].startswith("# Richpanel"):
        raise ValueError("Richpanel Workable llms.txt identity changed")
    match = re.search(
        rf"All open roles \(GET `?(https://apply\.workable\.com/{WORKABLE_ACCOUNT}/jobs\.md)`?\):"
        r"\s*(\d+)\s+current openings",
        text,
    )
    if match is None:
        raise ValueError("Richpanel Workable llms.txt no longer states its open-role count")
    departments = {
        found.group(1).strip(): int(found.group(2))
        for found in re.finditer(r"^- ([^:\n]+):\s*(\d+) roles?$", text, re.M)
    }
    return {
        "board_url": match.group(1),
        "open_positions": int(match.group(2)),
        "departments": departments,
        "company_url": WORKABLE_COMPANY_URL if WORKABLE_COMPANY_URL in text else None,
    }


def markdown_to_text(value: str) -> str:
    # Workable markdown keeps the office-gallery images as whole image lines whose
    # URLs may contain escaped parentheses, so drop those lines before inline rules.
    value = re.sub(r"(?m)^\s*!\[[^\]]*\].*$", " ", value or "")
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"</?[a-z][^>]*>", " ", value, flags=re.I)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s*(?:[-*+]|\d+\.)\s+", "", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return normalized_text(value)


def parse_workable_detail_md(text: str, job_id: str) -> dict:
    if not SHORTCODE.fullmatch(job_id or ""):
        raise ValueError(f"Invalid Richpanel Workable job id: {job_id!r}")
    lines = (text or "").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError(f"Richpanel Workable detail markdown has no title: {job_id}")
    title = normalized_text(lines[0][2:])
    meta_line = next((line for line in lines if line.startswith("> ")), "")
    meta = [part.strip() for part in meta_line[2:].split("·")]
    if not meta or normalized_text(meta[0]) != WORKABLE_ACCOUNT_NAME:
        raise ValueError(f"Richpanel Workable detail markdown company changed: {job_id}")
    posted = None
    for part in meta[1:]:
        found = re.fullmatch(r"Posted (\d{4}-\d{2}-\d{2})", part)
        if found:
            posted = found.group(1)
    salary = None
    workplace = None
    department = None
    for line in lines[:20]:
        found = re.fullmatch(r"\*\*Salary:\*\*\s*(.+)", line.strip())
        if found:
            salary = normalized_text(found.group(1))
        found = re.fullmatch(r"\*\*Workplace:\*\*\s*([a-z_]+)", line.strip())
        if found:
            workplace = found.group(1)
        found = re.fullmatch(r"\*\*Department:\*\*\s*(.+)", line.strip())
        if found:
            department = normalized_text(found.group(1))

    start = next((i for i, line in enumerate(lines) if line.strip().lower() == "## description"), None)
    end = next((i for i, line in enumerate(lines) if line.strip().lower() == "## apply"), None)
    if start is None or end is None or end <= start:
        raise ValueError(f"Richpanel Workable detail markdown body markers changed: {job_id}")
    body_lines = []
    for line in lines[start + 1:end]:
        if line.strip().lower() in WRAPPER_HEADINGS:
            continue
        body_lines.append(line)
    body_markdown = "\n".join(body_lines).strip()
    body_text = markdown_to_text(body_markdown)

    apply_url = None
    found = re.search(
        rf"\[Apply at Richpanel\]\((https://apply\.workable\.com/{WORKABLE_ACCOUNT}/j/([0-9A-Z]{{10}})/apply)\)",
        text,
    )
    if found:
        apply_url = found.group(1)
        if found.group(2) != job_id:
            raise ValueError(f"Richpanel Workable detail markdown apply link has wrong id: {job_id}")
    return {
        "job_id": job_id,
        "title": title,
        "company": normalized_text(meta[0]),
        "location": meta[1] if len(meta) > 1 else None,
        "employment_type": meta[2] if len(meta) > 2 else None,
        "date_posted": posted,
        "salary": salary,
        "workplace": workplace,
        "department": department,
        "body_markdown": body_markdown,
        "body_text": body_text,
        "body_tokens": len(body_tokens(body_text)),
        "body_tokens_sha256": sha256("\x1f".join(body_tokens(body_text)).encode()).hexdigest(),
        "body_sha256": sha256(body_text.encode()).hexdigest(),
        "sections": description_sections(markdown_to_html(body_markdown)),
        "apply_url": apply_url,
    }


def markdown_to_html(value: str) -> str:
    """Minimal markdown -> HTML used only to reuse description_sections on detail markdown."""
    value = (value or "").replace("**", "").replace("__", "")
    html_lines, list_tag = [], None
    for line in (value or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if list_tag:
                html_lines.append(f"</{list_tag}>")
                list_tag = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            if list_tag:
                html_lines.append(f"</{list_tag}>")
                list_tag = None
            level = len(heading.group(1))
            html_lines.append(f"<h{level}>{heading.group(2)}</h{level}>")
            continue
        if re.match(r"^[-*+]\s+", stripped):
            if list_tag != "ul":
                if list_tag:
                    html_lines.append(f"</{list_tag}>")
                html_lines.append("<ul>")
                list_tag = "ul"
            html_lines.append("<li>" + re.sub(r"^[-*+]\s+", "", stripped) + "</li>")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            if list_tag != "ol":
                if list_tag:
                    html_lines.append(f"</{list_tag}>")
                html_lines.append("<ol>")
                list_tag = "ol"
            html_lines.append("<li>" + re.sub(r"^\d+\.\s+", "", stripped) + "</li>")
            continue
        if list_tag:
            html_lines.append(f"</{list_tag}>")
            list_tag = None
        html_lines.append(f"<p>{stripped}</p>")
    if list_tag:
        html_lines.append(f"</{list_tag}>")
    return "\n".join(html_lines)


def choose_description(site_text: str | None, workable_text: str | None,
                       threshold: float = BODY_SIMILARITY_THRESHOLD) -> dict:
    """Pick the company-published body that actually describes this posting."""
    site, workable = token_set(site_text), token_set(workable_text)
    similarity = len(site & workable) / len(site | workable) if site and workable else None
    if site_text and workable_text:
        if similarity is not None and similarity < threshold:
            return {
                "selected": "workable",
                "reason": "company_detail_page_body_describes_different_role",
                "similarity": round(similarity, 6),
                "threshold": threshold,
            }
        return {
            "selected": "site",
            "reason": "company_detail_page_body_matches_exact_posting",
            "similarity": round(similarity, 6),
            "threshold": threshold,
        }
    if site_text:
        return {
            "selected": "site",
            "reason": "no_exact_workable_posting_for_card",
            "similarity": None,
            "threshold": threshold,
        }
    return {
        "selected": "workable",
        "reason": "site_detail_body_missing",
        "similarity": None,
        "threshold": threshold,
    }


def parse_salary(value: str | None) -> dict | None:
    if not value:
        return None
    match = SALARY.fullmatch(normalized_text(value))
    if match is None:
        return {"raw": value}
    def amount(text):
        return int(text.replace(",", ""))
    return {
        "raw": value,
        "currency": match.group("currency"),
        "min": amount(match.group("min")),
        "max": amount(match.group("max")),
        "period": "year",
    }


def reconcile_membership(cards: list[dict], board: dict, widget: dict, llms: dict) -> dict:
    """Reconcile the official cards with the company-scoped Workable listing."""
    errors: list[str] = []
    board_ids = [row["job_id"] for row in board["rows"]]
    widget_ids = [row["shortcode"] for row in widget["rows"]]
    if any(code is None for code in board_ids):
        errors.append("workable_board_row_without_job_id")
    if len(set(board_ids)) != len(board_ids):
        errors.append("workable_board_duplicate_ids")
    if set(board_ids) != set(widget_ids) or len(board_ids) != len(widget_ids):
        errors.append("workable_board_and_widget_membership_mismatch")
    if llms["open_positions"] != len(widget_ids):
        errors.append("workable_llms_count_mismatch")
    if llms["board_url"] != WORKABLE_BOARD_MD:
        errors.append("workable_llms_board_url_changed")
    if sum(llms["departments"].values()) not in (0, len(widget_ids)):
        errors.append("workable_llms_department_count_mismatch")

    card_ids = {
        card["slug"]: (card.get("site_detail") or {}).get("workable_id")
        for card in cards
    }
    card_shortcodes = {code for code in card_ids.values() if code}
    workable_only = [code for code in widget_ids if code not in card_shortcodes]
    site_only = [code for slug, code in card_ids.items() if code and code not in set(widget_ids)]
    unresolved_cards = [slug for slug, code in card_ids.items() if not code]

    ordered_ids = []
    for card in cards:
        code = card_ids[card["slug"]]
        if code and code not in ordered_ids:
            ordered_ids.append(code)
    for code in board_ids:
        if code and code not in ordered_ids:
            ordered_ids.append(code)
    return {
        "official_cards": len(cards),
        "official_card_ids": [card["slug"] for card in cards],
        "official_card_titles": [card["title"] for card in cards],
        "card_shortcodes": card_ids,
        "workable_board_rows": len(board_ids),
        "workable_widget_rows": len(widget_ids),
        "workable_board_ids": board_ids,
        "workable_widget_ids": widget_ids,
        "workable_llms_open_positions": llms["open_positions"],
        "site_only_ids": site_only,
        "workable_only_ids": workable_only,
        "cards_without_workable_link": unresolved_cards,
        "union_ids": ordered_ids,
        "union_count": len(ordered_ids),
        "errors": errors,
        "complete": not errors and not unresolved_cards,
    }


class Richpanel(Adapter):
    async def run(self):
        result = self.result
        result.adapter = self.source.adapter
        started = time.monotonic()
        metadata = result.metadata
        metadata["verification_revision"] = "richpanel.webflow-workable.1"
        metadata["official_entry_url"] = OFFICIAL_ENTRY
        metadata["listing_url"] = LISTING_URL
        metadata["workable_account"] = WORKABLE_ACCOUNT
        metadata["workable_account_name"] = WORKABLE_ACCOUNT_NAME
        metadata["request_scope"] = (
            "www.richpanel.com/careers and its /careers/<slug> detail pages, plus the exact "
            "company-scoped Workable account richpanel-1 linked by those pages; no aggregator, "
            "guessed ATS slug or parent-company board."
        )
        failed_sources: list[dict] = []
        detail_failures: list[dict] = []
        timing_parts: dict[str, float] = {}

        async def fetch(url: str, label: str):
            request_started = time.monotonic()
            try:
                response = await self.http.get(url)
            except Exception as exc:  # network, status, WAF: recorded exactly
                failed_sources.append({
                    "url": url,
                    "stage": label,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                return None
            request_seconds = max(0.0, time.monotonic() - request_started)
            metadata.setdefault("request_seconds", []).append(
                {"url": url, "stage": label, "seconds": round(request_seconds, 6)}
            )
            return response

        # 1. Official company listing.
        source_started = time.monotonic()
        listing = None
        response = await fetch(OFFICIAL_ENTRY, "official_listing")
        if response is not None:
            try:
                listing = parse_official_entry(response.text, str(response.url))
                listing["status"] = response.status_code
                listing["response_sha256"] = _response_sha(response)
                listing["response_bytes"] = len(response.content or b"")
                metadata["initial_official_listing"] = {
                    **listing, "cards": [dict(card) for card in listing["cards"]],
                }
            except ValueError as exc:
                failed_sources.append({
                    "url": OFFICIAL_ENTRY,
                    "stage": "official_listing_parse",
                    "reason": f"ValueError: {exc}",
                })

        # 2. Company-scoped Workable account: board markdown, llms index, widget rows.
        board_response = await fetch(WORKABLE_BOARD_MD, "workable_board")
        llms_response = await fetch(WORKABLE_LLMS, "workable_llms")
        widget_response = await fetch(WORKABLE_WIDGET_API, "workable_widget")

        board = widget = llms = None
        if board_response is not None:
            try:
                board = parse_workable_board_md(board_response.text)
                board["response_sha256"] = _response_sha(board_response)
            except ValueError as exc:
                failed_sources.append({"url": WORKABLE_BOARD_MD, "stage": "workable_board_parse",
                                       "reason": f"ValueError: {exc}"})
        if llms_response is not None:
            try:
                llms = parse_workable_llms(llms_response.text)
                llms["response_sha256"] = _response_sha(llms_response)
            except ValueError as exc:
                failed_sources.append({"url": WORKABLE_LLMS, "stage": "workable_llms_parse",
                                       "reason": f"ValueError: {exc}"})
        if widget_response is not None:
            try:
                widget = parse_workable_widget(widget_response.json())
                widget["response_sha256"] = _response_sha(widget_response)
            except ValueError as exc:
                failed_sources.append({"url": WORKABLE_WIDGET_API, "stage": "workable_widget_parse",
                                       "reason": f"ValueError: {exc}"})
        timing_parts["source_collection_wall_seconds"] = max(0.0, time.monotonic() - source_started)

        cards = listing["cards"] if listing else []
        membership = None
        if board is not None and widget is not None and llms is not None:
            membership = reconcile_membership(cards, board, widget, llms)
            metadata["membership_reconciliation"] = membership
            result.expected_total = membership["union_count"] if membership["complete"] else None
            if not membership["complete"]:
                result.errors.extend(membership["errors"])
        else:
            metadata["membership_reconciliation"] = {
                "complete": False,
                "reason": "workable_account_listing_unavailable",
                "site_cards": len(cards),
            }
            if cards:
                metadata["expected_total_source"] = "official_cards_only"
        if listing is None:
            result.errors.append("official_listing_unavailable")

        # 3. Detail phase: company detail pages, exact Workable markdown, exact apply links.
        detail_started = time.monotonic()
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        site_details: dict[str, dict] = {}
        workable_details: dict[str, dict] = {}
        apply_checks: dict[str, dict] = {}

        async def site_detail(card):
            async with semaphore:
                response = await fetch(card["url"], f"company_detail:{card['slug']}")
            if response is None:
                detail_failures.append({"id": card["slug"], "url": card["url"],
                                        "reason": "company_detail_fetch_failed"})
                return
            try:
                parsed = parse_site_detail(response.text, str(response.url))
            except ValueError as exc:
                detail_failures.append({"id": card["slug"], "url": card["url"],
                                        "reason": f"company_detail_parse_failed: {exc}"})
                return
            parsed["status"] = response.status_code
            parsed["response_sha256"] = _response_sha(response)
            site_details[card["slug"]] = parsed

        async def workable_detail(row):
            code = row["shortcode"]
            url = WORKABLE_DETAIL_MD.format(job_id=code)
            async with semaphore:
                response = await fetch(url, f"workable_detail:{code}")
            if response is None:
                detail_failures.append({"id": code, "url": url,
                                        "reason": "workable_detail_fetch_failed"})
                return
            try:
                parsed = parse_workable_detail_md(response.text, code)
            except ValueError as exc:
                detail_failures.append({"id": code, "url": url,
                                        "reason": f"workable_detail_parse_failed: {exc}"})
                return
            parsed["url"] = url
            parsed["status"] = response.status_code
            parsed["response_sha256"] = _response_sha(response)
            workable_details[code] = parsed

        async def apply_check(card):
            target = (card.get("apply_urls") or [None])[0]
            if not target:
                return
            async with semaphore:
                request_started = time.monotonic()
                try:
                    response = await self.http.get(target)
                except Exception as exc:
                    apply_checks[card["slug"]] = {
                        "url": target,
                        "ok": False,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                    return
                apply_checks[card["slug"]] = {
                    "url": target,
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "seconds": round(max(0.0, time.monotonic() - request_started), 6),
                    "ok": "not_found" not in str(response.url),
                }

        tasks = [site_detail(card) for card in cards]
        tasks += [workable_detail(row) for row in (widget["rows"] if widget else [])]
        if tasks:
            await asyncio.gather(*tasks)
        # A card becomes workable-resolvable only after its own detail page was read.
        for card in cards:
            detail = site_details.get(card["slug"])
            card["site_detail"] = detail
            card["apply_urls"] = (detail or {}).get("apply_urls") or []
            if detail:
                card["workable_id"] = detail.get("workable_id")

        if membership is not None and any(card.get("workable_id") for card in cards):
            membership = reconcile_membership(cards, board, widget, llms)
            metadata["membership_reconciliation"] = membership
            result.expected_total = membership["union_count"] if membership["complete"] else None
            if not membership["complete"]:
                result.errors.extend(
                    error for error in membership["errors"] if error not in result.errors
                )
        timing_parts["detail_phase_seconds"] = max(0.0, time.monotonic() - detail_started)

        # Verify every company-published apply link exactly (broken links recorded, not hidden).
        apply_tasks = [apply_check(card) for card in cards if card.get("workable_id")]
        if apply_tasks:
            await asyncio.gather(*apply_tasks)
        metadata["apply_link_checks"] = apply_checks
        card_by_slug = {card["slug"]: card for card in cards}
        apply_failures = [
            {
                "id": ((card_by_slug.get(slug, {}).get("site_detail") or {}).get("workable_id") or slug),
                "slug": slug,
                "url": check.get("url"),
                "final_url": check.get("final_url"),
                "reason": check.get("reason") or "apply_target_not_found",
            }
            for slug, check in apply_checks.items() if not check.get("ok")
        ]

        # 4. Build one record per distinct posting.
        widget_by_id = {row["shortcode"]: row for row in (widget["rows"] if widget else [])}
        board_by_id = {row["job_id"]: row for row in (board["rows"] if board else [])}
        ordered_ids = (membership or {}).get("union_ids") or []

        def card_for(code: str) -> dict | None:
            for card in cards:
                if (card.get("site_detail") or {}).get("workable_id") == code:
                    return card
            return None

        for code in ordered_ids:
            card = card_for(code)
            row = widget_by_id.get(code)
            board_row = board_by_id.get(code) or {}
            md = workable_details.get(code) or {}
            site = (card or {}).get("site_detail") or {}
            workable_text = row["description_text"] if row else ""
            site_text = site.get("body_text") or ""
            choice = choose_description(site_text, workable_text)

            if choice["selected"] == "site" and site.get("body_html"):
                description_html = site["body_html"]
                description_source_url = site["url"]
                description_method = "richpanel.company_detail_page"
            elif row is not None:
                description_html = row["description_html"]
                description_source_url = WORKABLE_DETAIL_MD.format(job_id=code)
                description_method = "richpanel.workable.widget_description+exact_markdown_detail"
            else:
                description_html = site.get("body_html") or ""
                description_source_url = site.get("url")
                description_method = "richpanel.company_detail_page"

            title = (card or {}).get("title") or (row or {}).get("title") or md.get("title") or ""
            job_url = (card or {}).get("url") or WORKABLE_JOB_URL.format(job_id=code)
            apply_url = None
            if card:
                apply_url = ((site.get("apply_urls") or [None])[0]
                             or WORKABLE_APPLY_URL.format(job_id=code))
            elif row:
                apply_url = row.get("application_url") or WORKABLE_APPLY_URL.format(job_id=code)

            locations = []
            if row:
                for location in row.get("locations") or []:
                    locations.append({
                        "display": md.get("location") or board_row.get("location"),
                        "city": location.get("city") or row.get("city"),
                        "region": location.get("region") or row.get("state"),
                        "country": location.get("country") or row.get("country"),
                        "country_code": location.get("countryCode"),
                        "hidden": location.get("hidden"),
                        "company_page": (card or {}).get("location"),
                    })
                if not locations:
                    locations.append({"display": md.get("location") or board_row.get("location"),
                                      "city": row.get("city"), "region": row.get("state"),
                                      "country": row.get("country"),
                                      "company_page": (card or {}).get("location")})
            if not locations and (card or {}).get("location"):
                locations.append({"display": card["location"]})

            posted = md.get("date_posted") or (row or {}).get("published_on")
            date_posted_raw = {
                "markdown_posted": md.get("date_posted"),
                "workable_published_on": (row or {}).get("published_on"),
                "workable_created_at": (row or {}).get("created_at"),
                "board_posted": board_row.get("posted"),
                "company_page_published_date": site.get("published_date"),
            }
            if date_posted_raw["markdown_posted"] and date_posted_raw["workable_published_on"] \
                    and date_posted_raw["markdown_posted"] != date_posted_raw["workable_published_on"]:
                result.errors.append(f"posting_date_mismatch:{code}")
                posted = None

            salary = parse_salary(md.get("salary"))
            if salary is None and board_row.get("salary"):
                salary = parse_salary(board_row["salary"])
            employment_type = (row or {}).get("employment_type") or md.get("employment_type") \
                or (card or {}).get("employment_type")
            department = (row or {}).get("department") or md.get("department") \
                or board_row.get("department") or (card or {}).get("department")
            sections = md.get("sections") or []
            if choice["selected"] == "site":
                sections = description_sections(description_html)

            description_text = text_html(description_html)
            markdown_tokens = body_tokens(md.get("body_text")) if md.get("body_text") else []
            stored_tokens = set(body_tokens(description_text))
            coverage = (
                sum(1 for token in markdown_tokens if token in stored_tokens) / len(markdown_tokens)
                if markdown_tokens else None
            )

            job = self.job(
                {
                    "listing_card": card,
                    "site_detail": site or None,
                    "workable": {key: value for key, value in (row or {}).items()
                                 if key not in ("description_html", "description_text")},
                    "workable_description_html_sha256": (
                        sha256((row or {}).get("description_html", "").encode()).hexdigest()
                        if row else None
                    ),
                    "board_row": board_row or None,
                    "markdown": {key: value for key, value in md.items()
                                 if key not in ("body_markdown", "sections")},
                    "markdown_body_markdown": md.get("body_markdown"),
                    "description": {
                        "selected": choice["selected"],
                        "reason": choice["reason"],
                        "similarity": choice.get("similarity"),
                        "similarity_threshold": choice.get("threshold"),
                        "site_body_chars": len(site_text),
                        "site_body_tokens": len(body_tokens(site_text)),
                        "site_body_sha256": site.get("body_sha256"),
                        "site_body_placeholder": site.get("placeholder_description"),
                        "workable_body_chars": len(workable_text),
                        "workable_body_tokens": len(body_tokens(workable_text)),
                        "workable_body_sha256": (
                            sha256(workable_text.encode()).hexdigest() if workable_text else None
                        ),
                        "markdown_body_tokens": len(markdown_tokens),
                        "markdown_token_coverage": round(coverage, 6) if coverage is not None else None,
                        "sections": sections,
                    },
                    "apply_check": apply_checks.get((card or {}).get("slug")),
                },
                title=title,
                url=job_url,
                job_id=code,
                description=description_html,
                method=description_method,
                description_url=description_source_url,
                description_text=description_text,
                locations=locations,
                department=department,
                employment_type=employment_type,
                workplace_type=WORKPLACE_LABELS.get(md.get("workplace") or ""),
                date_posted=posted,
                date_posted_raw=date_posted_raw,
                salary=salary,
                qualifications=_section_field(sections, r"requirement|qualification|great fit|good fit"),
                responsibilities=_section_field(sections, r"responsibilit"),
                experience=(row or {}).get("experience"),
                education=(row or {}).get("education"),
                employer=WORKABLE_ACCOUNT_NAME,
                apply_url=apply_url,
            )
            job.requisition_id = ((row or {}).get("code") or "").strip() or None
            if not card:
                job.warnings.append("company_page_card_absent_published_on_workable_account")
            if card and choice["selected"] == "workable":
                job.warnings.append("company_detail_page_body_describes_different_role")
            if card and not row:
                job.warnings.append("exact_workable_posting_closed_but_company_card_published")
            if apply_checks.get((card or {}).get("slug"), {}).get("ok") is False:
                job.warnings.append("apply_target_not_found")
            if coverage is not None and coverage < 0.98:
                job.warnings.append("markdown_token_coverage_below_0.98")
            self.add(job)

        # 5. Membership recheck after every detail request (shared listing could change).
        recheck_started = time.monotonic()
        if board is not None:
            recheck = await fetch(WORKABLE_BOARD_MD, "workable_board_recheck")
            if recheck is not None:
                try:
                    after = parse_workable_board_md(recheck.text)
                    before_ids = [row["job_id"] for row in board["rows"]]
                    after_ids = [row["job_id"] for row in after["rows"]]
                    metadata["final_membership_recheck"] = {
                        "before": len(before_ids), "after": len(after_ids),
                        "same_ids": set(before_ids) == set(after_ids),
                        "added": sorted(set(after_ids) - set(before_ids)),
                        "removed": sorted(set(before_ids) - set(after_ids)),
                        "response_sha256": _response_sha(recheck),
                    }
                    if set(before_ids) != set(after_ids):
                        result.errors.append("workable_membership_changed_during_run")
                except ValueError as exc:
                    result.errors.append(f"workable_membership_recheck_parse_failed: {exc}")
        timing_parts["final_recheck_wall_seconds"] = max(0.0, time.monotonic() - recheck_started)

        # 6. Certify only a fully reconciled capture.
        membership = metadata.get("membership_reconciliation") or {}
        listing_complete = bool(
            listing is not None
            and membership.get("complete") is True
            and result.expected_total == len(result.jobs)
            and not detail_failures
            and not result.errors
            and metadata.get("final_membership_recheck", {}).get("same_ids") is True
        )
        result.listing_complete = listing_complete
        result.metadata["detail_failures"] = detail_failures
        result.metadata["apply_link_failures"] = apply_failures
        result.metadata["failed_sources"] = failed_sources
        result.metadata["listing_authority"] = (
            "Richpanel's own www.richpanel.com/careers Webflow listing (3 cards, no pagination) "
            "union the company-scoped Workable account richpanel-1 open-roles listing linked by "
            "those cards (jobs.md + llms.txt + widget API each state 12)."
        )
        result.metadata["detail_method"] = (
            "Company detail page body when it describes the same posting; otherwise the exact "
            "company-linked Workable posting body, verified token-for-token against "
            "jobs/view/<shortcode>.md."
        )
        result.metadata["site_create_date_not_used"] = True
        result.timings.update(timing_parts)
        result.timings["overall_adapter_wall_seconds"] = max(0.0, time.monotonic() - started)
        result.metadata["timings"] = dict(result.timings)
        return result
