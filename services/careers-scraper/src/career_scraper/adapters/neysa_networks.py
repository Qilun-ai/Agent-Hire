"""Neysa Networks' WordPress careers journey and its company-scoped Kula board.

Observed live on 2026-11-07/08 (see ``companies/neysa-networks/journey-research.md``):

- Entry ``https://neysa.ai/careers/`` is the official WordPress careers page; its
  only job CTA is ``View Job Openings`` -> ``/careers/job-openings/``.
- The official listing is server-rendered by the company plugin
  ``neysa-kula-job-board``. It currently renders 20 cards, grouped by department,
  with search/department/location controls (the AJAX filter currently returns
  "No results found" and is not a membership source). There is no pagination.
- Every official card links to ``/careers/job-openings/?job_id=<id>``, an exact
  company-owned detail page with title, department, type, desired start date,
  location, minimum qualifications, the complete role body, and an exact
  ``Apply Now`` link to ``https://careers.kula.ai/neysa/<id>``.
- The company-scoped Kula board ``https://careers.kula.ai/neysa`` is the live
  listing behind that plugin/apply journey. It renders 29 cards and its exact
  account-scoped request is
  ``GET /api/internal/ats_job_posts?accountName=neysa&page=1&type=ats_job_post.index&items=99``
  with ``meta.count=29, meta.pages=1``. Every row is public
  (``listed=true``, ``on_career_page=true``, ``is_confidential=false``,
  ``end_at=null``). No other account or ATS board is queried.
- The official WordPress listing is a stale server-rendered subset: its newest
  card (id 64600) launched at 2026-10-29T04:41:55Z, while the oldest board-only
  card (id 64729) launched at 2026-10-29T06:28:46Z. All nine board-only postings
  have working official ``?job_id=`` detail pages, so they are not invented or
  aggregator-only. They are collected and marked as absent from the cached
  official listing.
- The exact Kula detail page for each id publishes a schema.org ``JobPosting``
  JSON-LD with ``datePosted`` (equal to the row's ``launch_at``), ``identifier``,
  ``hiringOrganization`` Neysa, ``jobLocation`` and the full description. That
  is the public posted date; the official page has no date field. No salary,
  requisition or legal-employer field is published, so those stay null.

The adapter fetches only the exact company URLs/API request observed above, plus
one official detail and one exact Kula detail page per listed id. It never
guesses an id, enumerates an ATS-wide board, or uses an aggregator.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://neysa.ai/careers/"
OFFICIAL_LISTING = "https://neysa.ai/careers/job-openings/"
OFFICIAL_DETAIL = OFFICIAL_LISTING + "?job_id={}"
KULA_BOARD = "https://careers.kula.ai/neysa"
KULA_API = "https://careers.kula.ai/api/internal/ats_job_posts"
KULA_FILTER_OPTIONS_API = "https://careers.kula.ai/api/internal/ats_job_posts/filter_options"
KULA_ACCOUNT = "neysa"
KULA_ITEMS = 99
JOB_LIST_TYPE = "ats_job_post.index"
FILTER_OPTIONS_TYPE = "ats_job_post.filter_options"
OFFICIAL_HOST = "neysa.ai"
KULA_HOST = "careers.kula.ai"
KULA_DETAIL_ROUTE = re.compile(r"^/neysa/(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$")
JOB_ID_ROUTE = re.compile(r"^\d+$")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def title_key(value: str | None) -> str:
    """Compare published titles across surfaces with different dash typography."""
    text = normalized_space(value)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def body_key(value: str | None) -> str:
    """Canonical text key for the exact role body across the three surfaces.

    The official WordPress body and the Kula JSON-LD/API body are text-identical
    except for invisible characters, typographic punctuation and two published
    ``24×7`` / ``24x7`` spellings. This key is used only for comparison; stored
    source HTML/text remains untouched.
    """
    text = text_html(value)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\u2018\u2019\u201a\u201b\u2032]", "'", text)
    text = re.sub(r"[\u201c\u201d\u201e\u201f\u2033]", '"', text)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = text.replace("\u00d7", "x")
    return re.sub(r"\s+", " ", text).strip()


def body_sha256(value: str | None) -> str:
    return hashlib.sha256(body_key(value).encode("utf-8")).hexdigest()


def visible_body_key(value: str | None) -> str:
    """Body key matching rendered innerText rather than HTML get_text spacing.

    ``text_html`` inserts a separating space around inline elements; a rendered
    browser body does not (``Neysa.ai,`` vs ``Neysa.ai ,``). This comparison key
    removes those synthetic spaces while preserving every visible word.
    """
    text = body_key(value)
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def compact_visible_key(value: str | None) -> str:
    """Whitespace-free visible-body key for independent rendered comparisons.

    Rendered innerText and server HTML can differ only in whether an inline
    element contributes a space (for example ``I<span>ndia</span>`` renders as
    ``India`` but HTML text extraction can yield ``I ndia``). Removing all
    whitespace keeps every visible character while ignoring that artifact.
    """
    return re.sub(r"\s+", "", visible_body_key(value))


def compact_visible_sha256(value: str | None) -> str:
    return hashlib.sha256(compact_visible_key(value).encode("utf-8")).hexdigest()


def compact_body_hash(html: str | None) -> str:
    """Whitespace-free NFC hash of the visible body, used by browser references."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso_value(value: str) -> str:
    if not isinstance(value, str) or not ISO_UTC.fullmatch(value):
        raise ValueError(f"Neysa source emitted an unexpected ISO timestamp: {value!r}")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _assert_https_host(url: str, host: str, *, path: str | None = None) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError(f"Neysa source URL left its inspected HTTPS host: {url}")
    if path is not None and parsed.path.rstrip("/") != path.rstrip("/"):
        raise ValueError(f"Neysa source URL has an unexpected path: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _assert_https_host(response_url, OFFICIAL_HOST, path="/careers")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at Neysa" not in title:
        raise ValueError("Neysa official careers entry no longer has its inspected title")
    links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        parsed = urlsplit(target)
        if (parsed.scheme == "https" and parsed.hostname == OFFICIAL_HOST
                and parsed.path.rstrip("/") == "/careers/job-openings"):
            links.append({"text": normalized_space(anchor.get_text(" ", strip=True)), "url": target})
    expected = "https://neysa.ai/careers/job-openings/"
    if len(links) != 1 or links[0]["url"] != expected or links[0]["text"] != "View Job Openings":
        raise ValueError("Neysa official careers entry no longer publishes one View Job Openings link")
    plugin_assets = sorted({
        urljoin(response_url, node.get("href", ""))
        for node in soup.select('link[href*="neysa-kula-job-board"]')
    })
    return {
        "url": response_url,
        "title": title,
        "job_openings_link": links[0],
        "kula_job_board_plugin_assets": plugin_assets,
    }


def parse_official_listing(html: str, response_url: str = OFFICIAL_LISTING) -> dict:
    _assert_https_host(response_url, OFFICIAL_HOST, path="/careers/job-openings")
    soup = BeautifulSoup(html, "lxml")
    wrapper = soup.select_one("div.job-listing-wrapper")
    if wrapper is None:
        raise ValueError("Neysa official listing no longer exposes div.job-listing-wrapper")
    section = wrapper.select_one("div.job-section")
    if section is None:
        raise ValueError("Neysa official listing no longer exposes div.job-section")

    cards, issues = [], []
    department = None
    seen = set()
    for node in section.children:
        if not isinstance(node, Tag):
            continue
        classes = node.get("class") or []
        if node.name == "h2" and "department-title" in classes:
            department = normalized_space(node.get_text(" ", strip=True))
            continue
        if node.name != "div" or "job-card" not in classes:
            continue
        title_node = node.select_one(".job-title")
        location_node = node.select_one(".details-highlight")
        anchor = node.select_one("a.job-btn")
        if title_node is None or location_node is None or anchor is None:
            issues.append("official_card_fields_missing")
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        parsed = urlsplit(target)
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST
                or parsed.path.rstrip("/") != "/careers/job-openings"
                or set(query) != {"job_id"} or len(query["job_id"]) != 1
                or not JOB_ID_ROUTE.fullmatch(query["job_id"][0])):
            issues.append(f"official_card_link_unexpected:{target}")
            continue
        identity = query["job_id"][0]
        if identity in seen:
            issues.append(f"official_card_duplicate_id:{identity}")
            continue
        seen.add(identity)
        cards.append({
            "id": identity,
            "title": normalized_space(title_node.get_text(" ", strip=True)),
            "location": normalized_space(location_node.get_text(" ", strip=True)),
            "department": department,
            "url": target,
            "cta": normalized_space(anchor.get_text(" ", strip=True)),
        })
    if not cards:
        issues.append("official_listing_zero_cards")
    department_options = [
        normalized_space(item.get("data-value", ""))
        for item in wrapper.select('.dropdown-item[data-category="department"]')
        if normalized_space(item.get("data-value", "")) not in ("", "All departments")
    ]
    location_options = [
        normalized_space(item.get("data-value", ""))
        for item in wrapper.select('.dropdown-item[data-category="location"]')
        if normalized_space(item.get("data-value", "")) not in ("", "All locations")
    ]
    search = wrapper.select_one("#category-input")
    pagination = [
        normalized_space(node.get_text(" ", strip=True))
        for node in soup.select("a,button")
        if re.search(r"\b(next|previous|load more|page \d+)\b", node.get_text(" ", strip=True), re.I)
    ]
    return {
        "url": response_url,
        "title": normalized_space(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "cards": cards,
        "ids": [card["id"] for card in cards],
        "count": len(cards),
        "departments": list(dict.fromkeys(card["department"] for card in cards if card["department"])),
        "department_options": list(dict.fromkeys(department_options)),
        "location_options": list(dict.fromkeys(location_options)),
        "search_placeholder": search.get("placeholder") if search else None,
        "pagination_controls": pagination,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_kula_board(html: str, response_url: str = KULA_BOARD) -> dict:
    _assert_https_host(response_url, KULA_HOST, path="/neysa")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Neysa" not in title:
        raise ValueError("Neysa company Kula board title no longer identifies Neysa")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    h1 = soup.select_one("h1")
    if h1 is None or normalized_space(h1.get_text(" ", strip=True)) != "Job Openings":
        raise ValueError("Neysa company Kula board no longer has the inspected Job Openings heading")
    cards, issues, seen = [], [], set()
    for anchor in soup.select('a[href*="/neysa/"]'):
        target = urljoin(response_url, anchor.get("href", ""))
        parsed = urlsplit(target)
        match = KULA_DETAIL_ROUTE.fullmatch(parsed.path)
        if match is None or parsed.scheme != "https" or parsed.hostname != KULA_HOST:
            continue
        identity, slug = match.groups()
        if identity in seen:
            issues.append(f"board_duplicate_id:{identity}")
            continue
        seen.add(identity)
        card = anchor.find_parent("div", class_="chakra-stack")
        paragraphs = card.select("p.chakra-text") if card else []
        if not card or not paragraphs:
            issues.append(f"board_card_fields_missing:{identity}")
            continue
        card_title = normalized_space(paragraphs[0].get_text(" ", strip=True))
        meta = normalized_space(paragraphs[1].get_text(" ", strip=True)) if len(paragraphs) > 1 else ""
        parts = [normalized_space(part) for part in meta.split("•")] if meta else []
        cards.append({
            "id": identity,
            "slug": slug,
            "title": card_title,
            "department_label": parts[0] if parts else None,
            "locations_label": parts[1] if len(parts) > 1 else None,
            "employment_label": parts[2] if len(parts) > 2 else None,
            "workplace_label": parts[3] if len(parts) > 3 else None,
            "canonical_url": target.split("?")[0],
            "cta": normalized_space(anchor.get_text(" ", strip=True)),
        })
    if not cards:
        issues.append("board_zero_cards")
    buttons = [normalized_space(button.get_text(" ", strip=True)) for button in soup.select("button")]
    buttons = [value for value in buttons if value]
    pagination = [
        value for value in buttons
        if re.search(r"\b(next|previous|load more|page \d+)\b", value, re.I)
    ]
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "cards": cards,
        "ids": [card["id"] for card in cards],
        "count": len(cards),
        "buttons": buttons,
        "pagination_controls": pagination,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_kula_api(payload, response_url: str = KULA_API) -> dict:
    _assert_https_host(response_url, KULA_HOST, path="/api/internal/ats_job_posts")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Neysa Kula listing response has no data array")
    meta = payload.get("meta") or {}
    rows = payload["data"]
    issues = []
    if not isinstance(meta.get("count"), int) or meta["count"] < 0:
        raise ValueError("Neysa Kula listing response has no valid meta.count")
    if meta.get("pages") != 1:
        issues.append(f"neysa_kula_page_count_changed:{meta.get('pages')!r}")
    if meta.get("page") != 1 or meta.get("items") != KULA_ITEMS:
        issues.append("neysa_kula_listing_page_shape_changed")
    seen = set()
    accounts = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Neysa Kula listing row is not an object")
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            raise ValueError("Neysa Kula listing row has an invalid id")
        identity = str(identity)
        if identity in seen:
            issues.append(f"api_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(row.get("title"), str) or not normalized_space(row["title"]):
            issues.append(f"api_title_missing:{identity}")
        if row.get("listed") is not True or row.get("on_career_page") is not True:
            issues.append(f"api_row_not_public:{identity}")
        if row.get("is_confidential") is not False:
            issues.append(f"api_row_confidential:{identity}")
        account = row.get("account_id")
        if isinstance(account, int):
            accounts.add(account)
        ats = row.get("ats_job")
        if not isinstance(ats, dict) or not isinstance(ats.get("job_description"), str):
            issues.append(f"api_description_missing:{identity}")
        try:
            _iso_value(row.get("launch_at"))
        except ValueError as exc:
            issues.append(str(exc))
        if row.get("end_at") is not None:
            try:
                _iso_value(row.get("end_at"))
            except ValueError as exc:
                issues.append(str(exc))
    if meta["count"] != len(rows):
        issues.append(f"api_meta_count_mismatch:{meta['count']}!= {len(rows)}")
    if len(accounts) != 1:
        issues.append("api_multiple_account_ids")
    return {
        "url": response_url,
        "rows": rows,
        "ids": [str(row["id"]) for row in rows],
        "count": meta["count"],
        "pages": meta.get("pages"),
        "page": meta.get("page"),
        "items": meta.get("items"),
        "account_ids": sorted(accounts),
        "row_fields": sorted({key for row in rows for key in row}),
        "ats_job_fields": sorted({key for row in rows for key in (row.get("ats_job") or {})}),
        "issues": list(dict.fromkeys(issues)),
    }


def parse_official_detail(html: str, response_url: str, job_id: str) -> dict:
    _assert_https_host(response_url, OFFICIAL_HOST, path="/careers/job-openings")
    query = parse_qs(urlsplit(response_url).query)
    if query != {"job_id": [str(job_id)]}:
        raise ValueError(f"Neysa official detail URL does not exactly carry job_id={job_id}")
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div.na-jd")
    if container is None:
        raise ValueError(f"Neysa official detail page does not render job id {job_id}")
    title_node = container.select_one("h1.job-title")
    department_node = container.select_one("p.job-department")
    apply_node = container.select_one("a.apply-btn")
    day = container.select_one("div.job-description div.day-in-life")
    if title_node is None or department_node is None or apply_node is None or day is None:
        raise ValueError(f"Neysa official detail page for {job_id} is missing inspected fields")
    apply_url = urljoin(response_url, apply_node.get("href", ""))
    parsed_apply = urlsplit(apply_url)
    if (parsed_apply.scheme != "https" or parsed_apply.hostname != KULA_HOST
            or parsed_apply.path != f"/neysa/{job_id}"):
        raise ValueError(f"Neysa official detail page for {job_id} has an unexpected apply link: {apply_url}")
    removed_heading = None
    first_heading = day.find("h2")
    if first_heading is not None and title_key(first_heading.get_text(" ", strip=True)) == "job description":
        removed_heading = normalized_space(first_heading.get_text(" ", strip=True))
        first_heading.decompose()
    body_html = day.decode_contents()
    body_text = text_html(body_html)
    if len(body_text.strip()) < 100:
        raise ValueError(f"Neysa official detail page for {job_id} has an implausibly short body")
    fields = {}
    for item in container.select(".detail-item"):
        label = item.select_one(".label")
        value = item.select_one(".value")
        if label is None or value is None:
            continue
        fields[normalized_space(label.get_text(" ", strip=True))] = normalized_space(
            value.get_text(" ", strip=True))
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "url": response_url,
        "canonical_url": canonical.get("href") if canonical else None,
        "title": normalized_space(title_node.get_text(" ", strip=True)),
        "department": normalized_space(department_node.get_text(" ", strip=True)),
        "apply_url": apply_url,
        "fields": fields,
        "detail_items": [
            {"label": normalized_space(item.select_one(".label").get_text(" ", strip=True)),
             "value": normalized_space(item.select_one(".value").get_text(" ", strip=True))}
            for item in container.select(".detail-item")
            if item.select_one(".label") and item.select_one(".value")
        ],
        "description_html": body_html,
        "description_text": body_text,
        "description_chars": len(body_text.strip()),
        "description_body_sha256": body_sha256(body_html),
        "description_body_fnv1a64": fnv1a64(body_key(body_html)),
        "description_compact_sha256": compact_body_hash(body_html),
        "removed_template_heading": removed_heading,
        "type": fields.get("Type"),
        "desired_start_date": fields.get("Desired Start Date"),
        "location": fields.get("Location"),
        "minimum_qualifications": fields.get("Minimum Qualifications"),
    }


def parse_kula_detail(html: str, response_url: str, job_id: str) -> dict:
    _assert_https_host(response_url, KULA_HOST)
    match = KULA_DETAIL_ROUTE.fullmatch(urlsplit(response_url).path)
    if match is None or match.group(1) != str(job_id):
        raise ValueError(f"Neysa Kula detail URL is not the exact exposed route for job {job_id}")
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            postings.append(value)
    if len(postings) != 1:
        raise ValueError(f"Neysa Kula detail page for {job_id} does not carry one JobPosting document")
    posting = postings[0]
    identifier = (posting.get("identifier") or {}).get("value")
    if str(identifier) != str(job_id):
        raise ValueError(f"Neysa Kula detail identifier changed for {job_id}: {identifier!r}")
    title = normalized_space(posting.get("title"))
    description_html = posting.get("description")
    if not title or not isinstance(description_html, str) or len(text_html(description_html).strip()) < 100:
        raise ValueError(f"Neysa Kula detail page for {job_id} has no usable title/full description")
    date_posted = _iso_value(posting.get("datePosted"))
    employer = (posting.get("hiringOrganization") or {}).get("name")
    employer_site = (posting.get("hiringOrganization") or {}).get("sameAs")
    if employer != "Neysa":
        raise ValueError(f"Neysa Kula detail employer changed for {job_id}: {employer!r}")
    if not isinstance(employer_site, str) or employer_site.rstrip("/") != "https://neysa.ai":
        raise ValueError(f"Neysa Kula detail employer site changed for {job_id}: {employer_site!r}")
    locations = []
    for place in posting.get("jobLocation") or []:
        address = (place or {}).get("address") or {}
        locations.append({
            "location": ", ".join(part for part in (
                address.get("addressLocality"), address.get("addressRegion"),
                address.get("addressCountry")) if part),
            "city": address.get("addressLocality"),
            "state": address.get("addressRegion"),
            "country": address.get("addressCountry"),
        })
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    return {
        "url": response_url,
        "canonical_url": canonical_url,
        "title": title,
        "date_posted": date_posted,
        "employment_type": posting.get("employmentType"),
        "identifier": posting.get("identifier"),
        "employer": employer,
        "employer_site": employer_site,
        "locations": locations,
        "direct_apply": posting.get("directApply"),
        "description_html": description_html,
        "description_text": text_html(description_html),
        "description_chars": len(text_html(description_html).strip()),
        "description_body_sha256": body_sha256(description_html),
        "description_body_fnv1a64": fnv1a64(body_key(description_html)),
        "description_compact_sha256": compact_body_hash(description_html),
        "job_posting": {key: value for key, value in posting.items() if key != "description"},
    }


def parse_kula_filter_options(payload) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Neysa Kula filter options response has no data object")
    data = payload["data"]
    departments = data.get("departments") or []
    offices = data.get("offices") or []
    if not isinstance(departments, list) or not isinstance(offices, list):
        raise ValueError("Neysa Kula filter options departments/offices are not arrays")
    return {
        "departments": [
            {"id": item.get("id"), "name": normalized_space(item.get("name")),
             "parent_id": item.get("parent_id"), "depth": item.get("depth")}
            for item in departments if isinstance(item, dict)
        ],
        "offices": [
            {"id": item.get("id"), "name": normalized_space(item.get("name")),
             "location": normalized_space(item.get("location")),
             "location_key": item.get("location_key")}
            for item in offices if isinstance(item, dict)
        ],
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-neysa-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-neysa-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_space(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_space(block.get_text(" ", strip=True))
            strong_text = normalized_space(strong.get_text(" ", strip=True)) if strong else ""
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
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def role_sections(html: str) -> tuple[list[dict], list[dict]]:
    """Select the employer-authored qualification/responsibility runs.

    A parent heading such as ``Key Responsibilities`` is often followed by
    numbered sub-headings; those sub-sections belong to the same run until a
    new top-level heading (e.g. ``What You Bring``) starts.
    """
    sections = description_sections(html)
    qualification_tokens = (
        "qualification", "requirement", "what you bring", "what you'll need",
        "what you will need", "skills", "who you are", "education",
    )
    responsibility_tokens = (
        "responsibilit", "what you will do", "what you'll do", "what you'll achieve",
        "the opportunity", "key responsibilities", "role summary", "about the role",
    )
    qualifications, responsibilities = [], []
    mode = None
    for section in sections:
        heading = (section["heading"] or "").casefold()
        if heading and any(token in heading for token in qualification_tokens):
            mode = "qualification"
            qualifications.append(section)
            continue
        if heading and any(token in heading for token in responsibility_tokens):
            mode = "responsibility"
            responsibilities.append(section)
            continue
        if mode == "responsibility" and re.match(r"^\d+[.)]\s", heading):
            responsibilities.append(section)
            continue
        if heading:
            mode = None
    return qualifications, responsibilities


def reconcile_membership(official: dict, api: dict, board: dict) -> dict:
    official_by_id = {card["id"]: card for card in official["cards"]}
    api_by_id = {str(row["id"]): row for row in api["rows"]}
    board_by_id = {card["id"]: card for card in board["cards"]}
    official_ids = set(official_by_id)
    api_ids = set(api_by_id)
    board_ids = set(board_by_id)
    board_only = [identity for identity in api["ids"] if identity not in official_ids]
    official_only = [identity for identity in official["ids"] if identity not in api_ids]
    title_mismatches = []
    department_mismatches = []
    for identity in sorted(official_ids & api_ids):
        card = official_by_id[identity]
        row = api_by_id[identity]
        if title_key(card["title"]) != title_key(row.get("title")):
            title_mismatches.append(identity)
        ats = row.get("ats_job") or {}
        department = (ats.get("ats_department") or {}).get("name")
        if card.get("department") and department and card["department"] != department:
            department_mismatches.append(identity)
    official_launches = [api_by_id[i].get("launch_at") for i in official_ids if i in api_by_id]
    extra_launches = [api_by_id[i].get("launch_at") for i in board_only if i in api_by_id]
    official_max = max((value for value in official_launches if value), default=None)
    extra_min = min((value for value in extra_launches if value), default=None)
    extras_all_newer = bool(
        official_max and extra_min and all(value and value > official_max for value in extra_launches)
    )
    complete = bool(
        official_ids and official_ids <= api_ids and board_ids == api_ids
        and not official_only and not title_mismatches and not department_mismatches
        and not api["issues"] and not board["issues"]
    )
    return {
        "official_listing_visible_ids": official["ids"],
        "official_listing_visible_count": official["count"],
        "company_board_ids": board["ids"],
        "company_board_count": board["count"],
        "company_api_ids": api["ids"],
        "company_api_count": api["count"],
        "official_listing_is_subset_of_live_board": official_ids <= api_ids,
        "board_and_api_membership_match": board_ids == api_ids,
        "official_only_ids": official_only,
        "board_only_ids": board_only,
        "official_card_title_mismatches": title_mismatches,
        "official_card_department_mismatches": department_mismatches,
        "official_listing_newest_launch_at": official_max,
        "board_only_oldest_launch_at": extra_min,
        "all_board_only_newer_than_official_listing_max": extras_all_newer,
        "official_listing_cache_gap_explanation": (
            "The server-rendered WordPress listing is a stale subset. All board-only ids have "
            "working official ?job_id= detail pages and are present on Neysa's company-scoped Kula board."
            if board_only else "No board-only ids observed."
        ),
        "complete": complete,
    }


class NeysaNetworks(Adapter):
    """Follow Neysa's own careers page, official details and company Kula board."""

    VERIFICATION_REVISION = "neysa-networks.official-wp-details+company-kula-board.1"

    @staticmethod
    def _api_url(page: int = 1) -> str:
        return f"{KULA_API}?accountName={KULA_ACCOUNT}&page={page}&type={JOB_LIST_TYPE}&items={KULA_ITEMS}"

    async def _load_listing(self, *, final: bool = False):
        label = "final" if final else "initial"
        entry_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        self._assert_company(entry_response.url, path="/careers")
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry_seconds = time.perf_counter() - entry_started

        listing_started = time.perf_counter()
        listing_response = await self.http.get(OFFICIAL_LISTING)
        self._assert_company(listing_response.url, path="/careers/job-openings")
        listing = parse_official_listing(listing_response.text, str(listing_response.url))
        listing_seconds = time.perf_counter() - listing_started

        board_started = time.perf_counter()
        board_response = await self.http.get(KULA_BOARD)
        if str(board_response.url) != KULA_BOARD:
            raise ValueError("Neysa company Kula board redirected away from its inspected URL")
        board = parse_kula_board(board_response.text, str(board_response.url))
        board_seconds = time.perf_counter() - board_started

        api_url = self._api_url()
        api_started = time.perf_counter()
        api_response = await self.http.get(api_url, headers={"Referer": KULA_BOARD})
        parsed_api = urlsplit(str(api_response.url))
        if (parsed_api.scheme != "https" or parsed_api.hostname != KULA_HOST
                or parsed_api.path != "/api/internal/ats_job_posts"
                or parse_qs(parsed_api.query) != {
                    "accountName": [KULA_ACCOUNT], "page": ["1"],
                    "type": [JOB_LIST_TYPE], "items": [str(KULA_ITEMS)],
                }):
            raise ValueError("Neysa company-scoped Kula listing request redirected or changed shape")
        api = parse_kula_api(api_response.json(), str(api_response.url))
        api_seconds = time.perf_counter() - api_started

        filter_started = time.perf_counter()
        filter_response = await self.http.get(
            KULA_FILTER_OPTIONS_API,
            params={"accountName": KULA_ACCOUNT, "type": FILTER_OPTIONS_TYPE},
            headers={"Referer": KULA_BOARD},
        )
        if (urlsplit(str(filter_response.url)).hostname != KULA_HOST
                or urlsplit(str(filter_response.url)).path != "/api/internal/ats_job_posts/filter_options"):
            raise ValueError("Neysa company filter-options request redirected away from its inspected route")
        filters = parse_kula_filter_options(filter_response.json())
        filter_seconds = time.perf_counter() - filter_started

        self.result.timings[f"{label}_entry_seconds"] = self.result.timings.get(
            f"{label}_entry_seconds", 0.0) + entry_seconds
        self.result.timings[f"{label}_official_listing_seconds"] = self.result.timings.get(
            f"{label}_official_listing_seconds", 0.0) + listing_seconds
        self.result.timings[f"{label}_kula_board_seconds"] = self.result.timings.get(
            f"{label}_kula_board_seconds", 0.0) + board_seconds
        self.result.timings[f"{label}_kula_api_seconds"] = self.result.timings.get(
            f"{label}_kula_api_seconds", 0.0) + api_seconds
        self.result.timings[f"{label}_filter_options_seconds"] = self.result.timings.get(
            f"{label}_filter_options_seconds", 0.0) + filter_seconds
        return {
            "label": label,
            "entry": entry,
            "official_listing": listing,
            "board": board,
            "api": api,
            "filter_options": filters,
            "seconds": {
                "official_entry": entry_seconds,
                "official_listing": listing_seconds,
                "kula_board": board_seconds,
                "kula_api": api_seconds,
                "kula_filter_options": filter_seconds,
            },
            "urls": {
                "entry": str(entry_response.url),
                "official_listing": str(listing_response.url),
                "kula_board": str(board_response.url),
                "kula_api": str(api_response.url),
                "kula_filter_options": str(filter_response.url),
            },
            "bytes": {
                "official_entry": len(entry_response.content),
                "official_listing": len(listing_response.content),
                "kula_board": len(board_response.content),
                "kula_api": len(api_response.content),
                "kula_filter_options": len(filter_response.content),
            },
            "sha256": {
                "official_entry": hashlib.sha256(entry_response.content).hexdigest(),
                "official_listing": hashlib.sha256(listing_response.content).hexdigest(),
                "kula_board": hashlib.sha256(board_response.content).hexdigest(),
                "kula_api": hashlib.sha256(api_response.content).hexdigest(),
                "kula_filter_options": hashlib.sha256(filter_response.content).hexdigest(),
            },
        }

    @staticmethod
    def _assert_company(url, *, path: str) -> None:
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST
                or parsed.path.rstrip("/") != path.rstrip("/")):
            raise ValueError(f"Neysa official page redirected away from its inspected route: {url}")

    def _skeleton(self, row: dict, official_card: dict | None, board_card: dict):
        identity = str(row["id"])
        ats = row.get("ats_job") or {}
        offices = [office for office in (ats.get("offices") or []) if isinstance(office, dict)]
        locations = [{
            "location": office.get("location"), "name": office.get("name"),
            "city": office.get("city"), "state": office.get("state"),
            "country": office.get("country"), "country_code": office.get("country_code"),
            "remote": office.get("remote"), "workplace": office.get("workplace"),
        } for office in offices]
        description_html = ats.get("job_description") or ""
        job = self.job(
            {
                "kula_listing_row": row,
                "official_listing_card": official_card,
                "kula_board_card": board_card,
                "source_surface": "company-scoped Kula board row; official and Kula detail pages hydrated next",
            },
            title=normalized_space(row.get("title")),
            url=OFFICIAL_DETAIL.format(identity),
            job_id=identity,
            requisition_id=None,
            apply_url=f"https://careers.kula.ai/neysa/{identity}",
            description=description_html,
            method="neysa.kula.ats_job_posts.ats_job.job_description",
            description_url=self._api_url(),
            locations=locations,
            department=(ats.get("ats_department") or {}).get("name"),
            employment_type=ats.get("work_type_name") or ats.get("employment_type"),
            workplace_type={"office": "On-Site", "remote": "Remote", "hybrid": "Hybrid"}.get(
                ats.get("workplace"), ats.get("workplace")),
            date_posted=row.get("launch_at"),
            date_posted_raw=row.get("launch_at"),
            date_updated=None,
            valid_through=row.get("end_at"),
            salary=None,
            employer="Neysa",
            language="en",
        )
        job.description_text = text_html(description_html)
        job.record_identity = f"neysa-networks:{identity}"
        job.raw["membership"] = {
            "in_official_listing": official_card is not None,
            "official_listing_is_stale_subset": official_card is None,
            "official_listing_detail_route": OFFICIAL_DETAIL.format(identity),
        }
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        row = job.raw["kula_listing_row"]
        board_card = job.raw["kula_board_card"]
        official_url = OFFICIAL_DETAIL.format(identity)
        canonical_url = board_card["canonical_url"]
        official_started = time.perf_counter()
        official_response = await self.http.get(official_url)
        official_seconds = time.perf_counter() - official_started
        official = parse_official_detail(official_response.text, str(official_response.url), identity)
        kula_started = time.perf_counter()
        kula_response = await self.http.get(canonical_url)
        kula_seconds = time.perf_counter() - kula_started
        if str(kula_response.url) != canonical_url:
            raise ValueError(f"Neysa Kula detail {identity} redirected away from its canonical route")
        kula = parse_kula_detail(kula_response.text, str(kula_response.url), identity)

        issues = []
        ats = row.get("ats_job") or {}
        api_title = normalized_space(row.get("title"))
        if title_key(api_title) != title_key(official["title"]):
            issues.append(f"official_title_mismatch:{official['title']!r}!={api_title!r}")
        if title_key(api_title) != title_key(kula["title"]):
            issues.append(f"kula_title_mismatch:{kula['title']!r}!={api_title!r}")
        api_department = (ats.get("ats_department") or {}).get("name")
        if official["department"] and api_department and official["department"] != api_department:
            issues.append(f"official_department_mismatch:{official['department']!r}!={api_department!r}")
        api_body = ats.get("job_description") or ""
        api_key = body_key(api_body)
        official_key = body_key(official["description_html"])
        kula_key = body_key(kula["description_html"])
        if official_key != api_key:
            issues.append("official_body_differs_from_company_api_body")
        if kula_key != api_key:
            issues.append("kula_detail_body_differs_from_company_api_body")
        if kula["date_posted"] != row.get("launch_at"):
            issues.append(f"kula_date_posted_differs_from_launch_at:{kula['date_posted']}!={row.get('launch_at')}")
        if (kula["identifier"] or {}).get("value") != identity:
            issues.append("kula_identifier_differs_from_listing_id")
        ats_offices = [office for office in (ats.get("offices") or []) if isinstance(office, dict)]
        api_cities = [office.get("city") for office in ats_offices if office.get("city")]
        kula_cities = [place.get("city") for place in kula["locations"] if place.get("city")]
        if api_cities and kula_cities and api_cities != kula_cities:
            issues.append(f"kula_locations_differ_from_api:{kula_cities}!={api_cities}")
        official_location = official.get("location") or ""
        if api_cities and not any(city in official_location for city in api_cities):
            issues.append(f"official_location_differs_from_api:{official_location!r}")
        if official["apply_url"] != f"https://careers.kula.ai/neysa/{identity}":
            issues.append(f"official_apply_url_changed:{official['apply_url']}")
        if official.get("type") and "full" not in official["type"].casefold():
            issues.append(f"official_employment_type_changed:{official.get('type')}")
        if issues:
            self.result.errors.extend(f"{identity}: {issue}" for issue in issues)

        qualifications, responsibilities = role_sections(official["description_html"])
        minimum_qualifications = official.get("minimum_qualifications")
        qualifications_payload = None
        if minimum_qualifications or qualifications:
            qualifications_payload = {
                "minimum_qualifications": minimum_qualifications,
                "sections": qualifications,
            }
        raw = {
            "kula_listing_row": row,
            "official_listing_card": job.raw.get("official_listing_card"),
            "kula_board_card": board_card,
            "official_detail": {
                "url": official_url,
                "final_url": str(official_response.url),
                "status": official_response.status_code,
                "request_seconds": official_seconds,
                "response_bytes": len(official_response.content),
                "response_sha256": hashlib.sha256(official_response.content).hexdigest(),
                "title": official["title"],
                "department": official["department"],
                "apply_url": official["apply_url"],
                "fields": official["fields"],
                "detail_items": official["detail_items"],
                "type": official.get("type"),
                "desired_start_date": official.get("desired_start_date"),
                "location": official.get("location"),
                "minimum_qualifications": minimum_qualifications,
                "description_chars": official["description_chars"],
                "description_body_sha256": official["description_body_sha256"],
                "description_body_fnv1a64": official["description_body_fnv1a64"],
                "description_compact_sha256": official["description_compact_sha256"],
                "removed_template_heading": official.get("removed_template_heading"),
                "canonical_url": official.get("canonical_url"),
            },
            "kula_detail": {
                "url": canonical_url,
                "final_url": str(kula_response.url),
                "status": kula_response.status_code,
                "request_seconds": kula_seconds,
                "response_bytes": len(kula_response.content),
                "response_sha256": hashlib.sha256(kula_response.content).hexdigest(),
                "title": kula["title"],
                "date_posted": kula["date_posted"],
                "employment_type": kula["employment_type"],
                "identifier": kula["identifier"],
                "employer": kula["employer"],
                "employer_site": kula["employer_site"],
                "locations": kula["locations"],
                "direct_apply": kula["direct_apply"],
                "description_chars": kula["description_chars"],
                "description_body_sha256": kula["description_body_sha256"],
                "description_body_fnv1a64": kula["description_body_fnv1a64"],
                "description_compact_sha256": kula["description_compact_sha256"],
                "job_posting": kula["job_posting"],
                "canonical_url": kula.get("canonical_url"),
            },
            "description_cross_check": {
                "company_api_body_chars": len(text_html(api_body).strip()),
                "company_api_body_sha256": body_sha256(api_body),
                "official_body_chars": official["description_chars"],
                "official_body_sha256": official["description_body_sha256"],
                "kula_body_chars": kula["description_chars"],
                "kula_body_sha256": kula["description_body_sha256"],
                "official_matches_company_api": official_key == api_key,
                "kula_matches_company_api": kula_key == api_key,
                "body_difference_scope": "typographic/invisible punctuation and published 24×7 vs 24x7 only",
            },
            "membership": {
                "in_official_listing": job.raw.get("official_listing_card") is not None,
                "official_listing_is_stale_subset": job.raw.get("official_listing_card") is None,
                "official_listing_detail_route": official_url,
                "company_board_row": True,
            },
            "role_sections": {
                "qualifications": qualifications,
                "responsibilities": responsibilities,
            },
        }
        description_html = official["description_html"]
        description_text = text_html(description_html)
        job.description_html = description_html
        job.description_text = description_text
        job.description_method = "neysa.official_job_openings.detail.role_body"
        job.description_source_url = official_url
        job.title = official["title"]
        job.url = official_url
        job.apply_url = official["apply_url"]
        job.locations = [
            {
                "location": office.get("location"), "name": office.get("name"),
                "city": office.get("city"), "state": office.get("state"),
                "country": office.get("country"), "country_code": office.get("country_code"),
                "remote": office.get("remote"), "workplace": office.get("workplace"),
            } for office in ats_offices
        ]
        job.department = official["department"]
        job.employment_type = official.get("type") or job.employment_type
        job.workplace_type = {
            "office": "On-Site", "remote": "Remote", "hybrid": "Hybrid"
        }.get(ats.get("workplace"), job.workplace_type)
        job.date_posted = kula["date_posted"]
        job.date_posted_raw = kula["date_posted"]
        job.valid_through = row.get("end_at")
        job.employer = kula["employer"]
        job.qualifications = qualifications_payload
        job.responsibilities = responsibilities or None
        job.raw = raw
        if issues:
            job.warnings.append("description_not_full_field")
        job.record_identity = f"neysa-networks:{identity}"
        job.description_verified = False
        return job

    def _summarize_listing(self, value: dict) -> dict:
        return {
            "url": value["url"],
            "title": value["title"],
            "count": value["count"],
            "ids": value["ids"],
            "departments": value.get("departments"),
            "pagination_controls": value.get("pagination_controls"),
            "issues": value.get("issues"),
        }

    def _summarize_api(self, value: dict) -> dict:
        return {
            "url": value["url"],
            "count": value["count"],
            "pages": value["pages"],
            "page": value["page"],
            "items": value["items"],
            "ids": value["ids"],
            "account_ids": value["account_ids"],
            "row_fields": value["row_fields"],
            "ats_job_fields": value["ats_job_fields"],
            "issues": value["issues"],
            "rows": [
                {
                    "id": str(row["id"]),
                    "title": normalized_space(row.get("title")),
                    "launch_at": row.get("launch_at"),
                    "end_at": row.get("end_at"),
                    "kind": row.get("kind"),
                    "listed": row.get("listed"),
                    "on_career_page": row.get("on_career_page"),
                    "is_confidential": row.get("is_confidential"),
                    "ats_department": (row.get("ats_job") or {}).get("ats_department"),
                    "offices": (row.get("ats_job") or {}).get("offices"),
                    "job_description_sha256": body_sha256((row.get("ats_job") or {}).get("job_description")),
                    "job_description_chars": len(text_html((row.get("ats_job") or {}).get("job_description")).strip()),
                }
                for row in value["rows"]
            ],
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_listing_url": OFFICIAL_LISTING,
            "company_scoped_listing_url": KULA_BOARD,
            "company_scoped_listing_api": self._api_url(),
            "membership_authority": (
                "Neysa's own careers page and the company-scoped Kula board/API it links "
                "(accountName=neysa). The 20-card WordPress listing is a stale subset; "
                "all 29 live board ids have working official ?job_id= detail pages."
            ),
            "detail_method": (
                "One official neysa.ai ?job_id= detail page and one exact Kula detail page "
                "per company-board id; full descriptions are cross-checked against the "
                "company-scoped API row."
            ),
            "request_scope": (
                "Only neysa.ai careers pages and careers.kula.ai accountName=neysa routes; "
                "no other account, no ATS-wide search, no aggregator."
            ),
        })
        try:
            initial = await self._load_listing()
            self.page_guard()
            api = initial["api"]
            listing = initial["official_listing"]
            board = initial["board"]
            recon = reconcile_membership(listing, api, board)
            self.result.expected_total = api["count"]
            self.result.metadata.update({
                "initial_official_entry": initial["entry"],
                "initial_official_listing": self._summarize_listing(listing),
                "initial_kula_board": {
                    "url": board["url"], "title": board["title"],
                    "canonical_url": board["canonical_url"], "count": board["count"],
                    "ids": board["ids"], "buttons": board["buttons"],
                    "pagination_controls": board["pagination_controls"],
                    "issues": board["issues"],
                    "cards": board["cards"],
                },
                "initial_company_api": self._summarize_api(api),
                "filter_options": initial["filter_options"],
                "membership_reconciliation": recon,
                "source_request_seconds": initial["seconds"],
                "source_response_bytes": initial["bytes"],
                "source_response_sha256": initial["sha256"],
            })
            if api["pages"] != 1:
                self.result.errors.append("neysa_native_pagination_changed; page 2 not requested")
            if api["issues"]:
                self.result.errors.extend(f"neysa_company_api: {issue}" for issue in api["issues"])
            if board["issues"]:
                self.result.errors.extend(f"neysa_kula_board: {issue}" for issue in board["issues"])
            if listing["issues"]:
                self.result.errors.extend(f"neysa_official_listing: {issue}" for issue in listing["issues"])
            if not recon["complete"]:
                self.result.errors.append("neysa_membership_reconciliation_incomplete")
            if not recon["official_listing_is_subset_of_live_board"]:
                self.result.errors.append("neysa_official_listing_has_ids_absent_from_live_company_board")
            if not recon["all_board_only_newer_than_official_listing_max"] and recon["board_only_ids"]:
                self.result.warnings.append("neysa_official_listing_cache_gap_not_temporally_explained")

            official_by_id = {card["id"]: card for card in listing["cards"]}
            board_by_id = {card["id"]: card for card in board["cards"]}
            jobs = []
            for row in api["rows"]:
                identity = str(row["id"])
                board_card = board_by_id.get(identity)
                if board_card is None:
                    self.result.errors.append(f"neysa_board_card_missing_for_api_row:{identity}")
                    continue
                job = self._skeleton(row, official_by_id.get(identity), board_card)
                self.add(job)
                jobs.append(job)
            if len(jobs) != api["count"]:
                self.result.errors.append("neysa_retained_rows_do_not_match_company_api_count")

            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started)
            hydrated = await self.hydrate(self.result.jobs, self._detail)
            self.result.jobs = list(hydrated)
        except Exception as exc:
            self.result.errors.append(f"Neysa source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        finally:
            self.result.timings.setdefault(
                "source_collection_wall_seconds", max(0.0, time.perf_counter() - source_started))

        if self.result.jobs:
            final_started = time.perf_counter()
            try:
                final = await self._load_listing(final=True)
                final_recon = reconcile_membership(
                    final["official_listing"], final["api"], final["board"])
                final_summary = self._summarize_api(final["api"])
                final_by_id = {str(row["id"]): row for row in final_summary["rows"]}
                initial_by_id = {str(row["id"]): row for row in
                                  self.result.metadata["initial_company_api"]["rows"]}
                changed = {
                    identity: sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
                    for identity in initial_by_id if identity in final_by_id
                }
                changed = {identity: fields for identity, fields in changed.items() if fields}
                same_ids = (self.result.metadata["initial_company_api"]["ids"]
                            == final["api"]["ids"])
                board_same = (self.result.metadata["initial_kula_board"]["ids"]
                              == final["board"]["ids"])
                listing_same = (self.result.metadata["initial_official_listing"]["ids"]
                                == final["official_listing"]["ids"])
                counts_same = (self.result.expected_total == final["api"]["count"])
                final_matches = bool(
                    same_ids and board_same and listing_same and counts_same
                    and not changed and final_recon["complete"]
                    and not final["api"]["issues"] and not final["board"]["issues"]
                    and not final["official_listing"]["issues"]
                )
                self.result.metadata["final_official_entry"] = final["entry"]
                self.result.metadata["final_official_listing"] = self._summarize_listing(
                    final["official_listing"])
                self.result.metadata["final_kula_board"] = {
                    "url": final["board"]["url"], "title": final["board"]["title"],
                    "canonical_url": final["board"]["canonical_url"],
                    "count": final["board"]["count"], "ids": final["board"]["ids"],
                    "buttons": final["board"]["buttons"],
                    "pagination_controls": final["board"]["pagination_controls"],
                    "issues": final["board"]["issues"],
                }
                self.result.metadata["final_company_api"] = final_summary
                self.result.metadata["final_membership_reconciliation"] = final_recon
                self.result.metadata["final_request_seconds"] = final["seconds"]
                self.result.metadata["final_response_sha256"] = final["sha256"]
                self.result.metadata["membership_reconciliation"].update({
                    "initial_final_ids_match": same_ids,
                    "initial_final_board_ids_match": board_same,
                    "initial_final_official_listing_ids_match": listing_same,
                    "all_company_api_row_fields_unchanged": not changed,
                    "changed_fields_by_id": changed,
                    "final_complete": final_matches,
                })
                if not same_ids:
                    self.result.errors.append("neysa_company_listing_membership_changed_during_collection")
                if not board_same:
                    self.result.errors.append("neysa_kula_board_membership_changed_during_collection")
                if not listing_same:
                    self.result.errors.append("neysa_official_listing_changed_during_collection")
                if changed:
                    self.result.errors.append("neysa_company_api_fields_changed_during_collection")
                if not counts_same:
                    self.result.errors.append("neysa_company_listing_count_changed_during_collection")
                if not final_matches:
                    self.result.errors.append("neysa_final_membership_recheck_incomplete")
                self.result.listing_complete = bool(
                    final_matches and len(self.result.jobs) == self.result.expected_total)
            except Exception as exc:
                self.result.errors.append(f"Neysa final source recheck failed: {type(exc).__name__}: {exc}")
                self.result.listing_complete = False
            finally:
                self.result.timings["final_recheck_wall_seconds"] = max(
                    0.0, time.perf_counter() - final_started)
        else:
            self.result.listing_complete = False

        self.result.metadata["job_ids"] = [str(job.job_id) for job in self.result.jobs]
        self.result.metadata["jobs_collected"] = len(self.result.jobs)
        self.result.metadata["descriptions_verified"] = sum(
            job.description_verified for job in self.result.jobs)
        self.result.metadata["official_listing_visible_jobs"] = self.result.metadata.get(
            "initial_official_listing", {}).get("count")
        self.result.metadata["company_board_visible_jobs"] = self.result.metadata.get(
            "initial_kula_board", {}).get("count")
        self.result.metadata["board_only_jobs"] = self.result.metadata.get(
            "membership_reconciliation", {}).get("board_only_ids", [])
        self.result.metadata["dates"] = {
            "date_posted_source": "Kula detail page schema.org JobPosting.datePosted; equals the company API row launch_at for every job",
            "date_updated": None,
            "valid_through": "API row end_at (null for all observed jobs)",
            "official_page_date_field": None,
        }
        self.result.metadata["salary"] = (
            "No structured salary field on the official page, Kula board/API row or Kula detail JSON-LD; null."
        )
        self.result.metadata["requisition_id"] = (
            "No separate public requisition field; the Kula posting id is retained as job_id; null."
        )
        self.result.metadata["employer"] = (
            "Public hiring organization 'Neysa' (sameAs https://neysa.ai); no separate legal-employer field."
        )
        self.result.timings["adapter_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        self.result.finish()
        return self.result
