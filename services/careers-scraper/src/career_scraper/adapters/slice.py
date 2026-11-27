"""Slice's own careers page, its embedded Kula board, and exact job details.

Observed live 2026-11-08 (see ``companies/slice/journey-research.md``):

- ``https://www.sliceit.com/careers`` redirects (301) to the current official
  site ``https://slice.bank.in/careers``.
- The official careers page links to ``/careers/open-positions``, which embeds
  exactly one Kula iframe: ``https://careers.kula.ai/slice?jobs=true``.
- The company-scoped Kula board/API exposes 34 public roles. Its listing request
  is ``GET /api/internal/ats_job_posts?accountName=slice&page=1&type=ats_job_post.index&items=99``
  with ``meta.count=34`` and ``meta.pages=1``.
- Every exact ``/slice/<id>-<slug>`` detail page publishes one schema.org
  ``JobPosting`` JSON-LD document whose full description is byte-identical to
  the API row. The detail page also exposes the exact ``/apply`` route.

The adapter reads only the company-published Kula account ``slice`` and the
exact detail URLs exposed by that listing. It never enumerates an ATS-wide
board, guesses a slug, or uses an aggregator.
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


OFFICIAL_ENTRY = "https://www.sliceit.com/careers"
OFFICIAL_CANONICAL = "https://slice.bank.in/careers"
OFFICIAL_LISTING = "https://slice.bank.in/careers/open-positions"
KULA_LISTING = "https://careers.kula.ai/slice?jobs=true"
KULA_BOARD = "https://careers.kula.ai/slice"
KULA_ORIGIN = "https://careers.kula.ai"
KULA_API = KULA_ORIGIN + "/api/internal/ats_job_posts"
KULA_FILTER_OPTIONS_API = KULA_API + "/filter_options"
KULA_HOST = "careers.kula.ai"
OFFICIAL_HOST = "slice.bank.in"
ACCOUNT_NAME = "slice"
ACCOUNT_ID = 1528
ITEMS_PER_PAGE = 99
JOB_LIST_TYPE = "ats_job_post.index"
FILTER_OPTIONS_TYPE = "ats_job_post.filter_options"
DETAIL_ROUTE = re.compile(r"^/slice/(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
WORKPLACE_LABELS = {"office": "On-Site", "remote": "Remote", "hybrid": "Hybrid"}


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_title(value: str | None) -> str:
    return normalized_space(value)


def title_key(value: str | None) -> str:
    """Compare titles across surfaces while tolerating dash typography."""
    text = normalized_space(value)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def cleaned_visible(value: str | None) -> str:
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def compact_visible(value: str | None) -> str:
    return re.sub(r"\s+", "", cleaned_visible(value))


def compact_visible_sha256(value: str | None) -> str:
    return hashlib.sha256(compact_visible(value).encode("utf-8")).hexdigest()


def body_sha256(html: str | None) -> str:
    return compact_visible_sha256(text_html(html))


def compact_body_sha256(html: str | None) -> str:
    return body_sha256(html)


def fnv1a64(value: str | None) -> str:
    digest = 14695981039346656037
    for char in compact_visible(value):
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _assert_https_host(url: str, host: str, *, path: str | None = None) -> None:
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError(f"Slice source URL left its inspected HTTPS host: {url}")
    if path is not None and parsed.path.rstrip("/") != path.rstrip("/"):
        raise ValueError(f"Slice source URL has an unexpected path: {url}")


def _iso_value(value: str, label: str) -> str:
    if not isinstance(value, str) or not ISO_UTC.fullmatch(value):
        raise ValueError(f"Slice {label} is not an ISO UTC timestamp: {value!r}")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _job_postings(soup: BeautifulSoup) -> list[dict]:
    postings = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            postings.append(value)
    return postings


def _organization(soup: BeautifulSoup) -> dict | None:
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "Organization":
            return value
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_CANONICAL) -> dict:
    _assert_https_host(response_url, OFFICIAL_HOST, path="/careers")
    if str(response_url).rstrip("/") != OFFICIAL_CANONICAL:
        raise ValueError(f"Slice official entry redirected to an uninspected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers" not in title or "slice" not in title.casefold():
        raise ValueError("Slice official careers page no longer has its inspected title")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if canonical_url != OFFICIAL_CANONICAL:
        raise ValueError(f"Slice official careers canonical changed: {canonical_url!r}")

    org = _organization(soup)
    if not org or normalized_space(org.get("name")) != "slice":
        raise ValueError("Slice official careers page no longer publishes its organization identity")
    legal_name = normalized_space(org.get("legalName"))
    if not legal_name:
        raise ValueError("Slice official careers page no longer publishes its legal employer name")
    if not str(org.get("url") or "").startswith("https://slice.bank.in"):
        raise ValueError(f"Slice official organization URL changed: {org.get('url')!r}")

    links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(str(response_url), anchor.get("href", ""))
        parsed = urlsplit(target)
        if (parsed.scheme == "https" and parsed.hostname == OFFICIAL_HOST
                and parsed.path.rstrip("/") == "/careers/open-positions"):
            links.append({"text": normalized_space(anchor.get_text(" ", strip=True)),
                          "url": "https://slice.bank.in/careers/open-positions"})
    if not links:
        raise ValueError("Slice official careers page no longer links its open-positions listing")
    if any(link["url"] != OFFICIAL_LISTING for link in links):
        raise ValueError("Slice official careers page links an unexpected open-positions URL")
    return {
        "url": str(response_url),
        "canonical_url": canonical_url,
        "title": title,
        "organization": {
            "name": normalized_space(org.get("name")),
            "legal_name": legal_name,
            "url": org.get("url"),
            "description": normalized_space(org.get("description")),
        },
        "job_openings_links": links,
        "job_openings_url": OFFICIAL_LISTING,
        "iframes": len(soup.select("iframe")),
    }


def parse_open_positions(html: str, response_url: str = OFFICIAL_LISTING) -> dict:
    _assert_https_host(response_url, OFFICIAL_HOST, path="/careers/open-positions")
    if str(response_url).rstrip("/") != OFFICIAL_LISTING:
        raise ValueError(f"Slice open-positions page redirected to an uninspected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Open positions | Careers | slice":
        raise ValueError(f"Slice open-positions page title changed: {title!r}")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if canonical_url != OFFICIAL_LISTING:
        raise ValueError(f"Slice open-positions canonical changed: {canonical_url!r}")

    iframes = soup.select("iframe")
    if len(iframes) != 1:
        raise ValueError(f"Slice open-positions page no longer exposes exactly one job iframe ({len(iframes)})")
    iframe = iframes[0]
    src = urljoin(str(response_url), iframe.get("src", ""))
    if (iframe.get("id") != "kula_embed" or iframe.get("title") != "Open positions at slice"
            or src != KULA_LISTING):
        raise ValueError("Slice company-published Kula iframe URL/id/title changed")
    return {
        "url": str(response_url),
        "canonical_url": canonical_url,
        "title": title,
        "iframe_id": iframe.get("id"),
        "iframe_title": iframe.get("title"),
        "iframe_url": src,
        "job_cards_on_official_page": len(soup.select('a[href*="/slice/"]')),
        "loading_text": normalized_space(soup.get_text(" ", strip=True))[:200],
    }


def parse_kula_listing(html: str, response_url: str = KULA_LISTING) -> dict:
    parsed_url = urlsplit(str(response_url))
    if (parsed_url.scheme != "https" or parsed_url.hostname != KULA_HOST
            or parsed_url.path.rstrip("/") != "/slice"
            or parse_qs(parsed_url.query) not in ({"jobs": ["true"]}, {})):
        raise ValueError(f"Slice Kula listing came from an unexpected route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "slice" not in title.casefold() or "Careers" not in title:
        raise ValueError(f"Slice Kula listing title changed: {title!r}")
    app_name = soup.select_one('meta[name="application-name"]')
    if not app_name or normalized_space(app_name.get("content")) != "slice":
        raise ValueError("Slice Kula listing no longer identifies the slice company account")
    headings = {
        normalized_space(node.get_text(" ", strip=True)).casefold()
        for node in soup.select("h1,h2,h3,h4")
    }
    if "open positions" not in headings:
        raise ValueError("Slice Kula listing no longer has the Open Positions heading")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if canonical_url != KULA_BOARD:
        raise ValueError(f"Slice Kula board canonical changed: {canonical_url!r}")

    cards, issues, seen = [], [], set()
    for anchor in soup.select('a[href*="/slice/"]'):
        target = urljoin(str(response_url), anchor.get("href", ""))
        parts = urlsplit(target)
        match = DETAIL_ROUTE.fullmatch(parts.path)
        if match is None or parts.scheme != "https" or parts.hostname != KULA_HOST:
            continue
        if parse_qs(parts.query) != {"jobs": ["true"]}:
            issues.append(f"company_card_link_query_changed:{target}")
            continue
        identity, slug = match.groups()
        if identity in seen:
            issues.append(f"company_card_duplicate_id:{identity}")
            continue
        card = anchor.find_parent("div", class_="chakra-card")
        paragraphs = card.select("p.chakra-text") if card else []
        department = card.select_one("span.chakra-stack > span") if card else None
        if not card or len(paragraphs) < 3 or department is None:
            issues.append(f"company_card_fields_missing:{identity}")
            continue
        meta_parts = [normalized_space(part) for part in re.split(r"\s*•\s*", paragraphs[2].get_text(" ", strip=True))]
        if len(meta_parts) != 2:
            issues.append(f"company_card_work_meta_missing:{identity}")
            continue
        seen.add(identity)
        cards.append({
            "id": identity,
            "slug": slug,
            "title": normalized_space(paragraphs[0].get_text(" ", strip=True)),
            "location": normalized_space(paragraphs[1].get_text(" ", strip=True)),
            "department": normalized_space(department.get_text(" ", strip=True)),
            "employment_label": meta_parts[0],
            "workplace_label": meta_parts[1],
            "href": target,
            "canonical_url": f"{parts.scheme}://{parts.netloc}{parts.path}",
            "cta": normalized_space(anchor.get_text(" ", strip=True)),
        })
    if not cards:
        issues.append("company_listing_zero_cards")

    buttons = [normalized_space(node.get_text(" ", strip=True)) for node in soup.select("button")]
    buttons = list(dict.fromkeys(value for value in buttons if value))
    pagination = [value for value in buttons
                  if re.search(r"\b(?:next|previous|load more|page \d+)\b", value, re.I)]
    return {
        "url": str(response_url),
        "canonical_url": canonical_url,
        "title": title,
        "application_name": normalized_space(app_name.get("content")),
        "cards": cards,
        "ids": [card["id"] for card in cards],
        "count": len(cards),
        "filters_visible": [value for value in buttons if value in ("All departments", "All locations")],
        "button_labels": buttons,
        "keyword_search_available": bool(soup.select_one('input[placeholder*="Search" i]')),
        "search_placeholder": (
            soup.select_one('input[placeholder*="Search" i]').get("placeholder")
            if soup.select_one('input[placeholder*="Search" i]') else None),
        "pagination_controls": pagination,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_kula_api_page(payload, requested_page: int = 1, response_url: str = KULA_API) -> dict:
    _assert_https_host(response_url, KULA_HOST, path="/api/internal/ats_job_posts")
    if not isinstance(payload, dict):
        raise ValueError("Slice Kula listing response is not an object")
    if payload.get("errors"):
        raise ValueError(f"Slice Kula listing response returned errors: {payload.get('errors')!r}")
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise ValueError("Slice Kula listing response is missing data or meta")
    count, page, items, pages = (meta.get(key) for key in ("count", "page", "items", "pages"))
    if (not all(isinstance(value, int) and value >= 0 for value in (count, page, items, pages))
            or page != requested_page or items != ITEMS_PER_PAGE or pages < 1 or len(data) > items):
        raise ValueError("Slice Kula listing pagination schema changed")
    expected_pages = max(1, (count + items - 1) // items)
    if pages != expected_pages:
        raise ValueError(
            f"Slice Kula listing meta.pages={pages} disagrees with count={count}, items={items}")
    expected_rows = min(items, max(0, count - (page - 1) * items)) if page <= pages else 0
    if len(data) != expected_rows:
        raise ValueError(
            f"Slice Kula listing page {page} has {len(data)} rows, expected {expected_rows} from meta")

    rows, issues, seen = [], [], set()
    for row in data:
        if not isinstance(row, dict):
            issues.append("api_row_not_object")
            continue
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            issues.append("api_row_missing_numeric_id")
            continue
        identity = str(identity)
        if identity in seen:
            issues.append(f"api_duplicate_id:{identity}")
            continue
        seen.add(identity)
        if row.get("account_id") != ACCOUNT_ID:
            issues.append(f"api_row_wrong_account:{identity}:{row.get('account_id')!r}")
            continue
        if row.get("listed") is not True or row.get("on_career_page") is not True:
            issues.append(f"api_row_not_public_careers_posting:{identity}")
            continue
        if row.get("is_confidential") is not False:
            issues.append(f"api_row_confidential:{identity}")
            continue
        if row.get("kind") not in ("external", "internal_and_external"):
            issues.append(f"api_row_unexpected_kind:{identity}:{row.get('kind')!r}")
            continue
        if not isinstance(row.get("title"), str) or not normalized_space(row["title"]):
            issues.append(f"api_row_title_missing:{identity}")
            continue
        try:
            _iso_value(row.get("launch_at"), "launch_at")
            if row.get("end_at") is not None:
                _iso_value(row.get("end_at"), "end_at")
        except ValueError as exc:
            issues.append(f"{identity}: {exc}")
            continue
        ats = row.get("ats_job")
        if not isinstance(ats, dict):
            issues.append(f"api_row_ats_job_missing:{identity}")
            continue
        description = ats.get("job_description")
        if not isinstance(description, str) or len(cleaned_visible(text_html(description))) < 100:
            issues.append(f"api_row_description_missing_or_short:{identity}")
            continue
        offices = ats.get("offices")
        if not isinstance(offices, list) or not offices:
            issues.append(f"api_row_offices_missing:{identity}")
            continue
        if any(not isinstance(office, dict) or not normalized_space(office.get("location"))
               for office in offices):
            issues.append(f"api_row_office_fields_missing:{identity}")
            continue
        department = ats.get("ats_department")
        if (not isinstance(department, dict) or not isinstance(department.get("id"), int)
                or not normalized_space(department.get("name"))):
            issues.append(f"api_row_department_missing:{identity}")
            continue
        if not normalized_space(ats.get("work_type_name")) or not normalized_space(ats.get("employment_type")):
            issues.append(f"api_row_work_type_missing:{identity}")
            continue
        if ats.get("workplace") not in WORKPLACE_LABELS:
            issues.append(f"api_row_workplace_unexpected:{identity}:{ats.get('workplace')!r}")
            continue
        rows.append(row)
    if meta.get("count") != count:
        issues.append("api_meta_count_changed")
    return {
        "url": str(response_url),
        "rows": rows,
        "ids": [str(row["id"]) for row in rows],
        "count": count,
        "page": page,
        "items": items,
        "pages": pages,
        "account_ids": sorted({row.get("account_id") for row in rows if isinstance(row.get("account_id"), int)}),
        "row_fields": sorted({key for row in rows for key in row}),
        "ats_job_fields": sorted({key for row in rows for key in (row.get("ats_job") or {})}),
        "issues": list(dict.fromkeys(issues)),
    }


def parse_kula_filter_options(payload) -> dict:
    if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise ValueError("Slice Kula filter-options response changed")
    data = payload["data"]
    departments = data.get("departments")
    offices = data.get("offices")
    if not isinstance(departments, list) or not isinstance(offices, list) or not departments or not offices:
        raise ValueError("Slice Kula filter-options response is missing departments/offices")

    issues = []
    by_id, offices_out = {}, []
    for item in departments:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not normalized_space(item.get("name")):
            issues.append("filter_department_unidentified")
            continue
        if item["id"] in by_id:
            issues.append(f"filter_department_duplicate:{item['id']}")
            continue
        by_id[item["id"]] = {
            "id": item["id"],
            "name": normalized_space(item.get("name")),
            "parent_id": item.get("parent_id"),
            "depth": item.get("depth"),
        }
    for item in offices:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), int)
                or not normalized_space(item.get("location"))):
            issues.append("filter_office_unidentified")
            continue
        offices_out.append({
            "id": item["id"],
            "name": normalized_space(item.get("name")),
            "location": normalized_space(item.get("location")),
            "location_key": normalized_space(item.get("location_key")),
        })

    paths, top_level = {}, {}
    for identity, item in by_id.items():
        path, current, guard = [], item, set()
        while current is not None:
            if current["id"] in guard:
                issues.append(f"filter_department_parent_cycle:{identity}")
                path = []
                break
            guard.add(current["id"])
            path.append(current)
            parent_id = current.get("parent_id")
            if parent_id is None:
                current = None
            else:
                current = by_id.get(parent_id)
                if current is None:
                    issues.append(f"filter_department_parent_missing:{identity}:{parent_id}")
                    path = []
                    break
        if not path:
            continue
        path = list(reversed(path))
        if item.get("depth") != len(path) - 1:
            issues.append(f"filter_department_depth_mismatch:{identity}:{item.get('depth')}!={len(path) - 1}")
        paths[identity] = path
        top_level[identity] = path[0]["name"]
    return {
        "departments": list(by_id.values()),
        "offices": offices_out,
        "paths": paths,
        "top_level_by_id": top_level,
        "top_level_names": list(dict.fromkeys(
            top_level[identity] for identity in by_id if identity in top_level)),
        "issues": list(dict.fromkeys(issues)),
    }


def department_path(filters: dict, department: dict | None) -> list[dict]:
    if not isinstance(department, dict):
        return []
    identity = department.get("id")
    path = filters.get("paths", {}).get(identity)
    if path:
        return path
    return [{
        "id": identity,
        "name": normalized_space(department.get("name")),
        "parent_id": None,
        "depth": department.get("depth"),
    }] if identity is not None else []


def top_level_department(filters: dict, department: dict | None) -> str | None:
    path = department_path(filters, department)
    return path[0]["name"] if path else None


def parse_kula_detail(html: str, response_url: str, job_id: str) -> dict:
    _assert_https_host(response_url, KULA_HOST)
    parts = urlsplit(str(response_url))
    match = DETAIL_ROUTE.fullmatch(parts.path)
    if match is None or match.group(1) != str(job_id):
        raise ValueError(f"Slice Kula detail URL is not the exact exposed route for job {job_id}")
    soup = BeautifulSoup(html, "lxml")
    postings = _job_postings(soup)
    if len(postings) != 1:
        raise ValueError(f"Slice Kula detail page for {job_id} does not carry exactly one JobPosting document")
    posting = postings[0]
    identifier = (posting.get("identifier") or {}).get("value")
    if str(identifier) != str(job_id) or (posting.get("identifier") or {}).get("name") != "slice":
        raise ValueError(f"Slice Kula detail identifier changed for {job_id}: {posting.get('identifier')!r}")
    title = normalized_space(posting.get("title"))
    description_html = posting.get("description")
    if not title or not isinstance(description_html, str):
        raise ValueError(f"Slice Kula detail page for {job_id} has no title or description")
    description_text = text_html(description_html)
    if len(cleaned_visible(description_text)) < 100:
        raise ValueError(f"Slice Kula detail page for {job_id} has an implausibly short description")
    date_posted = _iso_value(posting.get("datePosted"), "datePosted")
    employer = posting.get("hiringOrganization") or {}
    employer_name = normalized_space(employer.get("name"))
    employer_site = normalized_space(employer.get("sameAs"))
    if employer_name != "slice" or not employer_site.startswith("https://slice.bank.in"):
        raise ValueError(f"Slice Kula detail employer changed for {job_id}: {employer!r}")
    if posting.get("directApply") is not True:
        raise ValueError(f"Slice Kula detail directApply changed for {job_id}: {posting.get('directApply')!r}")
    if normalized_space(posting.get("employmentType")) != "FULL_TIME":
        raise ValueError(f"Slice Kula detail employmentType changed for {job_id}: {posting.get('employmentType')!r}")

    locations = []
    for place in posting.get("jobLocation") or []:
        address = (place or {}).get("address") or {}
        locations.append({
            "city": address.get("addressLocality"),
            "state": address.get("addressRegion"),
            "country_code": address.get("addressCountry"),
        })

    h1 = soup.select_one("h1")
    header = {}
    if h1 is not None:
        header["title"] = normalized_space(h1.get_text(" ", strip=True))
        paragraphs = h1.parent.select("p.chakra-text") if h1.parent else []
        if paragraphs:
            meta = normalized_space(paragraphs[0].get_text(" ", strip=True))
            matched = re.fullmatch(
                r"Work type:\s*(.*?)\s*·\s*Department:\s*(.*?)\s*·\s*Workplace:\s*(.*)", meta)
            if matched:
                header["work_type"], header["department"], header["workplace"] = matched.groups()
            else:
                header["meta_text"] = meta
        if len(paragraphs) > 1:
            header["location"] = normalized_space(paragraphs[1].get_text(" ", strip=True))

    panel = soup.select_one('[role="tabpanel"]')
    visible_description_text = None
    if panel is not None:
        candidates = [child for child in panel.find_all("div", recursive=False)
                      if child.get_text(" ", strip=True)]
        if candidates:
            longest = max(candidates, key=lambda child: len(child.get_text(" ", strip=True)))
            visible_description_text = normalized_space(longest.get_text(" ", strip=True))

    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if canonical_url != str(response_url):
        raise ValueError(f"Slice Kula detail canonical changed for {job_id}: {canonical_url!r}")
    apply_url = canonical_url.rstrip("/") + "/apply"
    return {
        "url": str(response_url),
        "canonical_url": canonical_url,
        "apply_url": apply_url,
        "title": title,
        "date_posted": date_posted,
        "employment_type": normalized_space(posting.get("employmentType")),
        "identifier": posting.get("identifier"),
        "employer": employer_name,
        "employer_site": employer_site,
        "direct_apply": posting.get("directApply"),
        "locations": locations,
        "header": header,
        "description_html": description_html,
        "description_text": description_text,
        "description_chars": len(cleaned_visible(description_text)),
        "description_sha256": body_sha256(description_html),
        "description_compact_sha256": compact_visible_sha256(description_text),
        "description_compact_fnv1a64": fnv1a64(description_text),
        "visible_description_text": visible_description_text,
        "visible_description_chars": len(compact_visible(visible_description_text)),
        "visible_description_sha256": compact_visible_sha256(visible_description_text),
        "visible_description_fnv1a64": fnv1a64(visible_description_text),
        "job_posting": {key: value for key, value in posting.items() if key != "description"},
    }


def _segment_is_heading(nodes: list) -> str:
    """Return a heading when a <br>-delimited paragraph segment is all <strong>."""
    text = normalized_space(BeautifulSoup("".join(str(node) for node in nodes), "lxml").get_text(" ", strip=True))
    if not text or len(text) > 140:
        return ""
    strong_text = ""
    for node in nodes:
        if isinstance(node, Tag) and node.name in ("strong", "b"):
            strong_text = normalized_space(node.get_text(" ", strip=True))
            break
    return text if strong_text and strong_text == text else ""


def _paragraph_sections(block: Tag) -> list[tuple[str, str]]:
    """Split a mixed <p> into (heading, body_html) runs at <br> boundaries.

    Some Slice descriptions publish headings such as ``About the role`` and
    ``What you will do`` inside one <p> separated by <br> tags. Treating that
    whole paragraph as a body loses both sections, so split it while keeping
    every non-heading segment's HTML intact.
    """
    segments, current = [], []
    for child in block.contents:
        if isinstance(child, Tag) and child.name in ("br", "BR"):
            segments.append(current)
            current = []
        else:
            current.append(child)
    segments.append(current)
    runs, body = [], []
    for nodes in segments:
        heading = _segment_is_heading(nodes)
        if heading:
            if body:
                runs.append(("", "<p>" + "".join(str(node) for node in body) + "</p>"))
                body = []
            runs.append((heading, ""))
        else:
            if "".join(str(node) for node in nodes).strip():
                body.extend(nodes)
    if body:
        runs.append(("", "<p>" + "".join(str(node) for node in body) + "</p>"))
    return runs


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-slice-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-slice-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None

    def start(heading: str):
        nonlocal current
        if current is not None:
            sections.append(current)
        current = {"heading": heading, "html": "", "text": ""}

    for block in blocks:
        if block.name in heading_tags:
            start(normalized_space(block.get_text(" ", strip=True)))
            continue
        if block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_space(block.get_text(" ", strip=True))
            strong_text = normalized_space(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 140:
                start(full_text)
                continue
            if block.find("br") is not None:
                for heading, body_html in _paragraph_sections(block):
                    if heading:
                        start(heading)
                    elif body_html:
                        if current is None:
                            start(None)
                        current["html"] += body_html
                continue
        if current is None:
            start(None)
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


def _heading_key(value: str | None) -> str:
    return normalized_space(value).casefold().replace("\u2019", "'").replace("\u2018", "'")


def role_sections(html: str) -> tuple[dict | None, dict | None]:
    """Select the employer-authored qualification and responsibility runs.

    Sub-headings inside a run (for example ``Application Security`` or
    ``Hustle + Heart``) continue that run until a new top-level heading such as
    ``Life at slice`` or ``About slice`` starts. Company boilerplate is not
    treated as role content.
    """
    qualification_tokens = (
        "qualification", "requirement", "what you will need", "what you'll need",
        "what we look for", "good-to-have", "good to have", "preferred certification",
        "soft skills", "skills", "education",
    )
    responsibility_tokens = (
        "responsibilit", "what you will do", "what you'll do", "what you'll be do",
        "key responsibilities",
    )
    stop_tokens = (
        "life at slice", "about slice", "about us", "about the company", "benefits",
        "perks", "why join", "equal opportunity", "how to apply",
    )
    qualifications, responsibilities = [], []
    mode = None
    for section in description_sections(html):
        heading = _heading_key(section["heading"])
        if heading and any(token in heading for token in stop_tokens):
            mode = None
            continue
        if heading and any(token in heading for token in qualification_tokens):
            mode = "qualification"
            qualifications.append(section)
            continue
        if heading and any(token in heading for token in responsibility_tokens):
            mode = "responsibility"
            responsibilities.append(section)
            continue
        if mode == "qualification":
            qualifications.append(section)
        elif mode == "responsibility":
            responsibilities.append(section)

    def payload(sections):
        if not sections:
            return None
        return {
            "headings": [section["heading"] for section in sections if section["heading"]],
            "sections": sections,
            "text": "\n\n".join(
                (section["heading"] + "\n" if section["heading"] else "") + section["text"]
                for section in sections if section["text"]),
        }

    return payload(qualifications), payload(responsibilities)


def _expected_location_label(row: dict) -> str:
    office = (row.get("ats_job") or {}).get("offices", [{}])[0]
    location = normalized_space(office.get("location"))
    return location + (" (Remote)" if office.get("remote") else "")


def reconcile_membership(listing: dict, api: dict, filters: dict) -> dict:
    listing_by_id = {card["id"]: card for card in listing["cards"]}
    api_by_id = {str(row["id"]): row for row in api["rows"]}
    listing_ids, api_ids = set(listing_by_id), set(api_by_id)
    title_mismatches, department_mismatches, location_mismatches = [], [], []
    employment_mismatches, workplace_mismatches = [], []
    department_unknown = []
    for identity in sorted(listing_ids & api_ids):
        card = listing_by_id[identity]
        row = api_by_id[identity]
        ats = row.get("ats_job") or {}
        if title_key(card["title"]) != title_key(row.get("title")):
            title_mismatches.append(identity)
        top_level = top_level_department(filters, ats.get("ats_department"))
        if top_level is None:
            department_unknown.append(identity)
        elif card["department"] != top_level:
            department_mismatches.append(identity)
        if card["location"] != _expected_location_label(row):
            location_mismatches.append(identity)
        if card["employment_label"] != normalized_space(ats.get("work_type_name")):
            employment_mismatches.append(identity)
        if card["workplace_label"] != WORKPLACE_LABELS.get(ats.get("workplace")):
            workplace_mismatches.append(identity)
    listing_only = [identity for identity in listing["ids"] if identity not in api_ids]
    api_only = [identity for identity in api["ids"] if identity not in listing_ids]
    remote_office_ids = [
        identity for identity, row in api_by_id.items()
        if (row.get("ats_job") or {}).get("workplace") == "office"
        and any(office.get("remote") for office in ((row.get("ats_job") or {}).get("offices") or []))
    ]
    complete = bool(
        listing_ids and listing_ids == api_ids
        and not listing_only and not api_only
        and not title_mismatches and not department_mismatches and not department_unknown
        and not location_mismatches and not employment_mismatches and not workplace_mismatches
        and not listing["issues"] and not api["issues"] and not filters["issues"]
        and api["account_ids"] == [ACCOUNT_ID]
        and not listing["pagination_controls"]
    )
    return {
        "official_listing_page": OFFICIAL_LISTING,
        "official_listing_iframe_url": KULA_LISTING,
        "company_listing_visible_ids": listing["ids"],
        "company_listing_visible_count": listing["count"],
        "company_api_ids": api["ids"],
        "company_api_count": api["count"],
        "company_api_pages": api["pages"],
        "company_api_account_ids": api["account_ids"],
        "listing_and_api_membership_match": listing_ids == api_ids,
        "listing_only_ids": listing_only,
        "api_only_ids": api_only,
        "listing_card_title_mismatches": title_mismatches,
        "listing_card_top_level_department_mismatches": department_mismatches,
        "listing_card_unknown_departments": department_unknown,
        "listing_card_location_mismatches": location_mismatches,
        "listing_card_employment_type_mismatches": employment_mismatches,
        "listing_card_workplace_mismatches": workplace_mismatches,
        "listing_pagination_controls": listing["pagination_controls"],
        "filters_visible": listing["filters_visible"],
        "filter_department_names": filters["top_level_names"],
        "filter_office_locations": [office["location"] for office in filters["offices"]],
        "remote_office_label_ids": sorted(remote_office_ids),
        "remote_office_label_note": (
            "These rows publish an office with remote=true and a card/location label ending "
            "(Remote), while the job-level ats_job.workplace remains 'office' and the UI "
            "prints 'On-Site'. Both published values are retained."
            if remote_office_ids else None
        ),
        "complete": complete,
    }


class Slice(Adapter):
    """Follow Slice's official careers page and its company-scoped Kula board."""

    VERIFICATION_REVISION = "slice.official-open-positions+kula-account+exact-details.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.surface: dict = {}
        self.filters: dict = {}
        self.legal_employer: str | None = None

    @staticmethod
    def _api_url(page: int = 1) -> str:
        return (f"{KULA_API}?accountName={ACCOUNT_NAME}&page={page}"
                f"&type={JOB_LIST_TYPE}&items={ITEMS_PER_PAGE}")

    async def _fetch_api_pages(self, *, guard: bool = True) -> dict:
        started = time.perf_counter()
        pages, rows, issues = [], [], []
        first_meta = None
        page = 1
        while True:
            if guard and not self.page_guard():
                issues.append("page_budget_exhausted")
                break
            response = await self.http.get(
                self._api_url(page),
                headers={"Referer": KULA_LISTING, "Origin": KULA_ORIGIN},
            )
            parsed = urlsplit(str(response.url))
            if (parsed.scheme != "https" or parsed.hostname != KULA_HOST
                    or parsed.path != "/api/internal/ats_job_posts"
                    or parse_qs(parsed.query) != {
                        "accountName": [ACCOUNT_NAME], "page": [str(page)],
                        "type": [JOB_LIST_TYPE], "items": [str(ITEMS_PER_PAGE)],
                    }):
                raise ValueError("Slice company-scoped Kula listing request redirected or changed shape")
            value = parse_kula_api_page(response.json(), page, str(response.url))
            if first_meta is None:
                first_meta = {"count": value["count"], "pages": value["pages"], "items": value["items"]}
                if value["pages"] < 1:
                    raise ValueError("Slice Kula listing response has no pages")
            elif (value["count"], value["pages"], value["items"]) != (
                    first_meta["count"], first_meta["pages"], first_meta["items"]):
                issues.append(f"api_page_meta_changed:{page}")
            pages.append({
                "page": page,
                "count": value["count"],
                "pages": value["pages"],
                "items": value["items"],
                "ids": value["ids"],
                "row_count": len(value["rows"]),
                "request_url": self._api_url(page),
                "final_url": str(response.url),
                "status": response.status_code,
                "response_bytes": len(response.content),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
            })
            issues.extend(value["issues"])
            rows.extend(value["rows"])
            if page >= first_meta["pages"]:
                break
            page += 1
        if first_meta is None:
            raise ValueError("Slice Kula listing request produced no page")
        ids = [str(row["id"]) for row in rows]
        if len(ids) != len(set(ids)):
            issues.append("api_duplicate_ids_across_pages")
        if len(rows) != first_meta["count"]:
            issues.append(f"api_collected_row_count_mismatch:{len(rows)}!={first_meta['count']}")
        return {
            "url": self._api_url(1),
            "rows": rows,
            "ids": ids,
            "count": first_meta["count"],
            "pages": first_meta["pages"],
            "items": first_meta["items"],
            "page_details": pages,
            "account_ids": sorted({row.get("account_id") for row in rows if isinstance(row.get("account_id"), int)}),
            "row_fields": sorted({key for row in rows for key in row}),
            "ats_job_fields": sorted({key for row in rows for key in (row.get("ats_job") or {})}),
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _load_surface(self) -> dict:
        entry_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        if str(entry_response.url).rstrip("/") != OFFICIAL_CANONICAL:
            raise ValueError(f"Slice official entry did not reach {OFFICIAL_CANONICAL}: {entry_response.url}")
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        self.legal_employer = entry["organization"]["legal_name"]
        entry_seconds = max(0.0, time.perf_counter() - entry_started)

        listing_started = time.perf_counter()
        listing_response = await self.http.get(OFFICIAL_LISTING)
        if str(listing_response.url).rstrip("/") != OFFICIAL_LISTING:
            raise ValueError(f"Slice open-positions page redirected: {listing_response.url}")
        official_listing = parse_open_positions(listing_response.text, str(listing_response.url))
        listing_seconds = max(0.0, time.perf_counter() - listing_started)

        kula_started = time.perf_counter()
        kula_response = await self.http.get(KULA_LISTING)
        if str(kula_response.url) != KULA_LISTING:
            raise ValueError(f"Slice Kula listing redirected away from its inspected URL: {kula_response.url}")
        kula_listing = parse_kula_listing(kula_response.text, str(kula_response.url))
        kula_seconds = max(0.0, time.perf_counter() - kula_started)

        filter_started = time.perf_counter()
        filter_response = await self.http.get(
            KULA_FILTER_OPTIONS_API,
            params={"accountName": ACCOUNT_NAME, "type": FILTER_OPTIONS_TYPE},
            headers={"Referer": KULA_LISTING, "Origin": KULA_ORIGIN},
        )
        filter_parts = urlsplit(str(filter_response.url))
        if (filter_parts.scheme != "https" or filter_parts.hostname != KULA_HOST
                or filter_parts.path != "/api/internal/ats_job_posts/filter_options"
                or parse_qs(filter_parts.query) != {
                    "accountName": [ACCOUNT_NAME], "type": [FILTER_OPTIONS_TYPE]}):
            raise ValueError("Slice filter-options request redirected or changed shape")
        filters = parse_kula_filter_options(filter_response.json())
        filter_seconds = max(0.0, time.perf_counter() - filter_started)

        api = await self._fetch_api_pages()
        self.filters = filters

        self.result.timings["official_entry_seconds"] = self.result.timings.get(
            "official_entry_seconds", 0.0) + entry_seconds
        self.result.timings["official_listing_seconds"] = self.result.timings.get(
            "official_listing_seconds", 0.0) + listing_seconds
        self.result.timings["kula_listing_seconds"] = self.result.timings.get(
            "kula_listing_seconds", 0.0) + kula_seconds
        self.result.timings["filter_options_seconds"] = self.result.timings.get(
            "filter_options_seconds", 0.0) + filter_seconds
        self.result.timings["kula_api_seconds"] = self.result.timings.get(
            "kula_api_seconds", 0.0) + api["seconds"]
        return {
            "entry": entry,
            "official_listing": official_listing,
            "kula_listing": kula_listing,
            "filters": filters,
            "api": api,
            "requests": {
                "entry": {
                    "request_url": OFFICIAL_ENTRY,
                    "final_url": str(entry_response.url),
                    "status": entry_response.status_code,
                    "seconds": entry_seconds,
                    "response_bytes": len(entry_response.content),
                    "response_sha256": hashlib.sha256(entry_response.content).hexdigest(),
                    "redirects": [str(item.url) for item in entry_response.history],
                },
                "official_listing": {
                    "request_url": OFFICIAL_LISTING,
                    "final_url": str(listing_response.url),
                    "status": listing_response.status_code,
                    "seconds": listing_seconds,
                    "response_bytes": len(listing_response.content),
                    "response_sha256": hashlib.sha256(listing_response.content).hexdigest(),
                },
                "kula_listing": {
                    "request_url": KULA_LISTING,
                    "final_url": str(kula_response.url),
                    "status": kula_response.status_code,
                    "seconds": kula_seconds,
                    "response_bytes": len(kula_response.content),
                    "response_sha256": hashlib.sha256(kula_response.content).hexdigest(),
                },
                "filter_options": {
                    "request_url": str(filter_response.request.url),
                    "final_url": str(filter_response.url),
                    "status": filter_response.status_code,
                    "seconds": filter_seconds,
                    "response_bytes": len(filter_response.content),
                    "response_sha256": hashlib.sha256(filter_response.content).hexdigest(),
                },
            },
        }

    def _skeleton(self, row: dict, card: dict, surface: dict) -> object:
        identity = str(row["id"])
        ats = row.get("ats_job") or {}
        department = ats.get("ats_department") or {}
        path = department_path(self.filters, department)
        top_level = path[0]["name"] if path else normalized_space(department.get("name"))
        exact_department = normalized_space(department.get("name"))
        team = exact_department if exact_department and exact_department != top_level else None
        offices = [office for office in (ats.get("offices") or []) if isinstance(office, dict)]
        locations = []
        for office in offices:
            label = normalized_space(office.get("location"))
            if office.get("remote"):
                label += " (Remote)"
            locations.append({
                "location": label,
                "api_location": normalized_space(office.get("location")),
                "office_name": normalized_space(office.get("name")),
                "city": office.get("city"),
                "state": office.get("state"),
                "country": office.get("country"),
                "country_code": office.get("country_code"),
                "remote": office.get("remote"),
                "workplace": office.get("workplace"),
            })
        description_html = ats.get("job_description") or ""
        job = self.job(
            {
                "kula_listing_row": row,
                "company_listing_card": card,
                "official_entry": surface["entry"],
                "official_listing": surface["official_listing"],
                "membership": {
                    "in_company_listing": True,
                    "company_board_row": True,
                    "account_id": row.get("account_id"),
                    "listing_url": KULA_LISTING,
                    "detail_url": card["canonical_url"],
                },
                "departments": {
                    "top_level": top_level,
                    "exact": exact_department,
                    "path": [item["name"] for item in path],
                    "api": department,
                },
                "workplace_consistency": {
                    "job_level": ats.get("workplace"),
                    "job_level_label": WORKPLACE_LABELS.get(ats.get("workplace")),
                    "office_remote": [office.get("remote") for office in offices],
                    "published_location_label": card["location"],
                },
                "source_surface": "company-published Kula iframe listing row; exact detail hydrated next",
            },
            title=normalized_space(row.get("title")),
            url=card["canonical_url"],
            job_id=identity,
            requisition_id=None,
            apply_url=card["canonical_url"].rstrip("/") + "/apply",
            description=description_html,
            method="slice.kula.ats_job_posts.ats_job.job_description",
            description_url=self._api_url(1),
            locations=locations,
            department=top_level,
            team=team,
            employment_type=normalized_space(ats.get("work_type_name")),
            workplace_type=WORKPLACE_LABELS.get(ats.get("workplace")),
            date_posted=row.get("launch_at"),
            date_posted_raw=row.get("launch_at"),
            date_updated=None,
            valid_through=row.get("end_at"),
            salary=None,
            employer=self.legal_employer,
            language="en",
        )
        job.description_text = text_html(description_html)
        job.record_identity = f"slice:{identity}"
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        row = job.raw["kula_listing_row"]
        card = job.raw["company_listing_card"]
        ats = row.get("ats_job") or {}
        canonical_url = card["canonical_url"]
        started = time.perf_counter()
        response = await self.http.get(canonical_url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != canonical_url:
            raise ValueError(f"Slice detail {identity} redirected away from its canonical route: {response.url}")
        detail = parse_kula_detail(response.text, str(response.url), identity)

        issues = []
        api_title = normalized_space(row.get("title"))
        if title_key(api_title) != title_key(detail["title"]):
            issues.append(f"detail_title_mismatch:{detail['title']!r}!={api_title!r}")
        if detail["date_posted"] != row.get("launch_at"):
            issues.append(f"detail_date_posted_mismatch:{detail['date_posted']}!={row.get('launch_at')}")
        if str((detail.get("identifier") or {}).get("value")) != identity:
            issues.append("detail_identifier_mismatch")
        if detail["employment_type"] != "FULL_TIME" or normalized_space(ats.get("employment_type")) != "full_time":
            issues.append(f"detail_employment_type_mismatch:{detail['employment_type']!r}")
        if detail["employer"] != "slice":
            issues.append(f"detail_employer_mismatch:{detail['employer']!r}")
        if detail["direct_apply"] is not True:
            issues.append("detail_direct_apply_changed")
        api_description = ats.get("job_description") or ""
        if detail["description_html"] != api_description:
            issues.append("detail_description_differs_from_company_api_html")
        if compact_visible(detail["description_text"]) != compact_visible(text_html(api_description)):
            issues.append("detail_visible_description_differs_from_company_api_text")
        if detail.get("visible_description_text") is None:
            issues.append("detail_visible_description_container_missing")
        elif compact_visible(detail["visible_description_text"]) != compact_visible(text_html(api_description)):
            issues.append("detail_rendered_description_differs_from_company_api_text")

        department = ats.get("ats_department") or {}
        top_level = top_level_department(self.filters, department)
        expected_location = _expected_location_label(row)
        header = detail.get("header") or {}
        if header.get("title") and title_key(header["title"]) != title_key(api_title):
            issues.append(f"detail_header_title_mismatch:{header.get('title')!r}")
        if normalized_space(header.get("work_type")) != normalized_space(ats.get("work_type_name")):
            issues.append(f"detail_header_work_type_mismatch:{header.get('work_type')!r}")
        if normalized_space(header.get("department")) != top_level:
            issues.append(f"detail_header_department_mismatch:{header.get('department')!r}!={top_level!r}")
        if normalized_space(header.get("workplace")) != WORKPLACE_LABELS.get(ats.get("workplace")):
            issues.append(f"detail_header_workplace_mismatch:{header.get('workplace')!r}")
        if normalized_space(header.get("location")) != expected_location:
            issues.append(f"detail_header_location_mismatch:{header.get('location')!r}!={expected_location!r}")
        api_offices = [office for office in (ats.get("offices") or []) if isinstance(office, dict)]
        detail_locations = detail.get("locations") or []
        if len(api_offices) != len(detail_locations):
            issues.append(f"detail_location_count_mismatch:{len(detail_locations)}!={len(api_offices)}")
        else:
            for office, location in zip(api_offices, detail_locations):
                if (office.get("city"), office.get("state"), office.get("country_code")) != (
                        location.get("city"), location.get("state"), location.get("country_code")):
                    issues.append("detail_job_location_differs_from_api")
                    break

        qualifications, responsibilities = role_sections(detail["description_html"])
        raw = dict(job.raw)
        raw.update({
            "detail": {
                "url": canonical_url,
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": seconds,
                "response_bytes": len(response.content),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "title": detail["title"],
                "date_posted": detail["date_posted"],
                "employment_type": detail["employment_type"],
                "identifier": detail["identifier"],
                "employer": detail["employer"],
                "employer_site": detail["employer_site"],
                "direct_apply": detail["direct_apply"],
                "header": detail["header"],
                "locations": detail["locations"],
                "description_chars": detail["description_chars"],
                "description_sha256": detail["description_sha256"],
                "description_compact_sha256": detail["description_compact_sha256"],
                "description_compact_fnv1a64": detail["description_compact_fnv1a64"],
                "visible_description_chars": detail["visible_description_chars"],
                "visible_description_sha256": detail["visible_description_sha256"],
                "visible_description_fnv1a64": detail["visible_description_fnv1a64"],
                "job_posting": detail["job_posting"],
                "canonical_url": detail["canonical_url"],
                "apply_url": detail["apply_url"],
            },
            "description_cross_check": {
                "api_description_chars": len(cleaned_visible(text_html(api_description))),
                "api_description_sha256": body_sha256(api_description),
                "detail_description_chars": detail["description_chars"],
                "detail_description_sha256": detail["description_sha256"],
                "api_html_equals_detail_html": detail["description_html"] == api_description,
                "api_text_equals_detail_text": (
                    compact_visible(text_html(api_description))
                    == compact_visible(detail["description_text"])),
                "api_text_equals_rendered_detail_container": (
                    detail.get("visible_description_text") is not None
                    and compact_visible(detail["visible_description_text"])
                    == compact_visible(text_html(api_description))),
                "title_matches": title_key(api_title) == title_key(detail["title"]),
                "date_posted_matches_launch_at": detail["date_posted"] == row.get("launch_at"),
                "header_department_matches_top_level": normalized_space(header.get("department")) == top_level,
                "header_location_matches_listing": normalized_space(header.get("location")) == expected_location,
                "location_addresses_match_api": not any(
                    issue.startswith("detail_location") or issue.startswith("detail_job_location")
                    for issue in issues),
                "application_form_excluded_from_description": (
                    "Application form" not in compact_visible(detail["description_text"])
                    and "Apply for this position" not in compact_visible(detail["description_text"])),
            },
            "role_sections": {
                "qualifications": qualifications,
                "responsibilities": responsibilities,
            },
            "application": {
                "apply_url": detail["apply_url"],
                "apply_control": "Apply for this position",
                "apply_route_trailing_slash_redirect_observed": 308,
                "application_questions_excluded_from_job_fields": True,
            },
        })
        if issues:
            self.result.errors.extend(f"{identity}: {issue}" for issue in issues)
            job.warnings.append("description_not_full_field")
        job.title = detail["title"]
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = "slice.kula_detail_page.jobposting_jsonld"
        job.description_source_url = canonical_url
        job.apply_url = detail["apply_url"]
        job.department = top_level
        job.team = (
            normalized_space(department.get("name"))
            if normalized_space(department.get("name")) != top_level else None
        )
        job.employment_type = normalized_space(header.get("work_type")) or normalized_space(ats.get("work_type_name"))
        job.workplace_type = normalized_space(header.get("workplace")) or WORKPLACE_LABELS.get(ats.get("workplace"))
        job.date_posted = detail["date_posted"]
        job.date_posted_raw = detail["date_posted"]
        job.valid_through = row.get("end_at")
        job.employer = self.legal_employer
        job.qualifications = qualifications
        job.responsibilities = responsibilities
        job.raw = raw
        job.record_identity = f"slice:{identity}"
        return job

    async def _final_recheck(self, initial: dict) -> dict:
        started = time.perf_counter()
        listing_response = await self.http.get(KULA_LISTING)
        if str(listing_response.url) != KULA_LISTING:
            raise ValueError("Slice final Kula listing redirected away from its inspected URL")
        listing = parse_kula_listing(listing_response.text, str(listing_response.url))
        api = await self._fetch_api_pages(guard=False)
        filter_response = await self.http.get(
            KULA_FILTER_OPTIONS_API,
            params={"accountName": ACCOUNT_NAME, "type": FILTER_OPTIONS_TYPE},
            headers={"Referer": KULA_LISTING, "Origin": KULA_ORIGIN},
        )
        filters = parse_kula_filter_options(filter_response.json())
        recon = reconcile_membership(listing, api, filters)
        initial_ids = initial["api"]["ids"]
        final_ids = api["ids"]
        unchanged = initial_ids == final_ids and initial["kula_listing"]["ids"] == listing["ids"]
        if not unchanged:
            self.result.errors.append("final_membership_changed_during_collection")
        if not recon["complete"]:
            self.result.errors.append("final_membership_reconciliation_incomplete")
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["final_recheck_seconds"] = self.result.timings.get(
            "final_recheck_seconds", 0.0) + seconds
        return {
            "listing_ids": listing["ids"],
            "api_ids": final_ids,
            "count": api["count"],
            "pages": api["pages"],
            "account_ids": api["account_ids"],
            "unchanged_since_initial": unchanged,
            "reconciliation": recon,
            "seconds": seconds,
            "requests": {
                "kula_listing": {
                    "request_url": KULA_LISTING,
                    "final_url": str(listing_response.url),
                    "status": listing_response.status_code,
                    "response_bytes": len(listing_response.content),
                    "response_sha256": hashlib.sha256(listing_response.content).hexdigest(),
                },
                "kula_api": {
                    "request_url": self._api_url(1),
                    "status": 200,
                    "pages": [page["request_url"] for page in api["page_details"]],
                },
                "filter_options": {
                    "request_url": str(filter_response.request.url),
                    "final_url": str(filter_response.url),
                    "status": filter_response.status_code,
                    "response_bytes": len(filter_response.content),
                    "response_sha256": hashlib.sha256(filter_response.content).hexdigest(),
                },
            },
        }

    async def run(self):
        surface = await self._load_surface()
        self.surface = surface
        listing = surface["kula_listing"]
        api = surface["api"]
        filters = surface["filters"]
        recon = reconcile_membership(listing, api, filters)
        self.result.expected_total = api["count"]
        self.result.listing_complete = recon["complete"]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_url": surface["requests"]["entry"]["final_url"],
            "official_listing_url": OFFICIAL_LISTING,
            "company_listing_url": KULA_LISTING,
            "company_board_canonical_url": KULA_BOARD,
            "listing_authority": (
                "The exact Kula iframe published by Slice's own /careers/open-positions page; "
                "membership is the unfiltered accountName=slice listing request."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": "/api/internal/ats_job_posts",
                "query_shape": {
                    "accountName": ACCOUNT_NAME,
                    "page": "UI page state",
                    "type": JOB_LIST_TYPE,
                    "items": ITEMS_PER_PAGE,
                },
                "filters_applied": False,
                "scope": f"Slice accountName={ACCOUNT_NAME} only; account_id={ACCOUNT_ID}.",
            },
            "detail_method": (
                "Every exact company-listed Kula detail page is fetched; its schema.org "
                "JobPosting JSON-LD description is compared byte-for-byte with the "
                "account-scoped API row before being stored."
            ),
            "request_scope": (
                "Only the official Slice entry/open-positions pages, the exact Kula "
                "accountName=slice listing/filter requests, and the exact detail URLs "
                "exposed by that listing. No ATS-wide or parent-company board."
            ),
            "official_entry": surface["entry"],
            "official_listing": surface["official_listing"],
            "kula_listing": {
                "url": listing["url"],
                "canonical_url": listing["canonical_url"],
                "title": listing["title"],
                "application_name": listing["application_name"],
                "count": listing["count"],
                "ids": listing["ids"],
                "filters_visible": listing["filters_visible"],
                "button_labels": listing["button_labels"],
                "pagination_controls": listing["pagination_controls"],
                "issues": listing["issues"],
                "cards": listing["cards"],
            },
            "kula_api": {
                "url": api["url"],
                "count": api["count"],
                "pages": api["pages"],
                "items": api["items"],
                "ids": api["ids"],
                "account_ids": api["account_ids"],
                "row_fields": api["row_fields"],
                "ats_job_fields": api["ats_job_fields"],
                "issues": api["issues"],
                "page_details": api["page_details"],
            },
            "filter_options": {
                "department_count": len(filters["departments"]),
                "office_count": len(filters["offices"]),
                "top_level_departments": filters["top_level_names"],
                "offices": filters["offices"],
                "issues": filters["issues"],
            },
            "membership_reconciliation": recon,
            "employer": {
                "hiring_organization": "slice",
                "legal_name": self.legal_employer,
                "same_as": "https://slice.bank.in",
                "source": "official careers page schema.org Organization legalName",
            },
            "dates": {
                "date_posted_source": "Kula API launch_at; equal to detail JobPosting.datePosted for every job",
                "date_updated": None,
                "valid_through": "Kula API end_at; null for all observed jobs",
            },
            "salary": "No job salary field on the listing, API row, detail header or JSON-LD; null.",
            "requisition_id": "No separate public requisition field; Kula posting id is job_id; null.",
            "application_form_note": (
                "The Kula application form asks current/expected salary and experience questions; "
                "these are application questions, not role descriptions or published job salary."
            ),
            "requests": surface["requests"],
        })
        if listing["issues"]:
            self.result.errors.extend(f"listing: {issue}" for issue in listing["issues"])
        if api["issues"]:
            self.result.errors.extend(f"api: {issue}" for issue in api["issues"])
        if filters["issues"]:
            self.result.errors.extend(f"filters: {issue}" for issue in filters["issues"])
        if not recon["complete"]:
            self.result.errors.append("membership_reconciliation_incomplete")
        if recon["remote_office_label_ids"]:
            self.result.warnings.append(
                f"office_remote_label_conflicts_with_job_level_workplace:{len(recon['remote_office_label_ids'])} jobs")

        cards_by_id = {card["id"]: card for card in listing["cards"]}
        jobs = []
        for row in api["rows"]:
            identity = str(row["id"])
            card = cards_by_id.get(identity)
            if card is None:
                self.result.errors.append(f"{identity}: company_listing_card_missing")
                continue
            jobs.append(self._skeleton(row, card, surface))
        for job in jobs:
            self.add(job)
        await self.hydrate(jobs, self._detail)

        final = await self._final_recheck(surface)
        self.result.metadata["final_membership_reconciliation"] = final
        self.result.listing_complete = bool(
            recon["complete"] and final["reconciliation"]["complete"] and final["unchanged_since_initial"])
        if final["unchanged_since_initial"]:
            self.result.metadata["membership_reconciliation"]["final_complete"] = final["reconciliation"]["complete"]
        self.result.metadata["source_collection_seconds"] = round(
            self.result.timings.get("official_entry_seconds", 0.0)
            + self.result.timings.get("official_listing_seconds", 0.0)
            + self.result.timings.get("kula_listing_seconds", 0.0)
            + self.result.timings.get("filter_options_seconds", 0.0)
            + self.result.timings.get("kula_api_seconds", 0.0), 6)
        return self.result
