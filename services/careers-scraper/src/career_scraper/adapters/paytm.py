"""Paytm's official careers page and the exact Lever board it publishes.

Observed live journey (2026-11-08, see ``companies/paytm/journey-research.md``):

- Entry ``https://paytm.com/careers/`` is Paytm's own careers page. It has no
  redirect and publishes multiple "Apply Now" / "View All Roles" anchors whose
  target is exactly ``https://jobs.lever.co/paytm``. No other job board or ATS
  link exists on the page; the footer's ``/fse/`` field-sales landing page is a
  generic role advertisement with a Google Form, not a posting list, and is
  deliberately not treated as membership.
- ``GET https://jobs.lever.co/paytm`` SSR-renders every open posting card in one
  page (201 cards on the inspected date), grouped under department/team
  headers. Cards carry ``data-qa-posting-id=<UUID>``, a title, and
  workplace/commitment/location spans. The filter bar (Location type, Location,
  Team, Work type) only links to query-string subsets of the same board; there
  is no pagination or "Load more" control.
- ``GET https://api.lever.co/v0/postings/paytm?mode=json`` is the board's own
  unfiltered JSON listing request. It returns the same 201 IDs and is used here
  only to reconcile membership and to retain the source's richer categories
  (department, team, allLocations, country, createdAt). The board HTML remains
  the membership authority.
- Each exact ``https://jobs.lever.co/paytm/<UUID>`` detail page carries one
  ``JobPosting`` JSON-LD document. Its ``description`` is the full employer
  body: role description plus every ``lists[]`` section plus the closing
  "Why join?" block and, when published, the salary range. It excludes Lever's
  generic ``data-qa="ai-disclaimer"`` page chrome and the Apply buttons. The
  JSON-LD text was reconciled against the rendered job-description,
  posting-requirements, closing-description and salary-range sections for all
  201 postings.
- ``datePosted`` is published in the JSON-LD and equals the board row's
  ``createdAt`` UTC date for every posting. It is used as ``date_posted``;
  ``createdAt`` is preserved verbatim in ``raw``. Lever publishes no updated or
  expiry date, so those fields stay null.

The adapter therefore validates the official entry and its exact board link,
fetches the unfiltered board and its matching JSON listing, hydrates every
visible card from its exact detail URL, and reconciles the initial/final board
and API IDs with the exported records.
"""
from __future__ import annotations

from hashlib import sha256
import html
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY_URL = "https://paytm.com/careers/"
ENTRY_CANONICAL = "https://paytm.com/careers"
ENTRY_HOSTS = {"paytm.com", "www.paytm.com"}
BOARD_URL = "https://jobs.lever.co/paytm"
BOARD_API_URL = "https://api.lever.co/v0/postings/paytm?mode=json"
BOARD_HOST = "jobs.lever.co"
BOARD_SLUG = "paytm"
DETAIL_URL_TEMPLATE = BOARD_URL + "/{job_id}"
APPLY_URL_TEMPLATE = BOARD_URL + "/{job_id}/apply"
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
AI_DISCLAIMER_MARKER = "We may use artificial intelligence (AI) tools"
WORKPLACE_LABELS = {"onsite": "On-site", "remote": "Remote", "hybrid": "Hybrid"}
RESPONSIBILITY_HEADING = re.compile(
    r"(?:what you(?:'|’)?.{0,3}(?:will|’ll|'ll)? ?(?:be )?do(?:ing)?\b|"
    r"responsibilities|key responsibilities|the role|your role|what you will do)",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"(?:what you(?:'|’)?.{0,3}(?:will|’ll|'ll)? ?(?:be )?(?:need|bring|looking for)\b|"
    r"what we(?:'|’)re looking for|requirements|qualifications|skills|"
    r"must have|nice to have|good to have|who you are)",
    re.I,
)


def _clean(value):
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _literal_newlines(value):
    """The JSON-LD source sometimes contains literal ``\\n`` text.

    Those characters are an artifact of the employer's HTML, not part of the
    rendered body. Normalise them before extracting text so a hash comparison
    against the rendered DOM is meaningful.
    """
    return (value or "").replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")


def normalized_text(value):
    return text_html(_literal_newlines(value))


def compact_body_hash(value):
    text = unicodedata.normalize("NFC", value or "")
    text = re.sub(r"[\s\ufeff]", "", text)
    return sha256(text.encode("utf-8")).hexdigest()


def _strip_trailing_disclaimer_html(description_html, disclaimer_text):
    """Remove a trailing copy of the page's generic AI notice from a role body.

    One inspected posting pasted the same generic AI disclaimer into its own
    job-description section while the page also rendered the standard
    ``data-qa="ai-disclaimer"`` block.  The notice is page chrome, not role
    content, so keep it in raw evidence but exclude it from the stored body.
    """
    if not description_html or not disclaimer_text:
        return description_html
    soup = BeautifulSoup(f'<div data-paytm-body="1">{description_html}</div>', "lxml")
    root = soup.select_one("[data-paytm-body]")
    if root is None:
        return description_html
    target = compact_body_hash(disclaimer_text)
    candidates = [
        node for node in root.find_all(["p", "div", "li", "section"])
        if compact_body_hash(normalized_text(str(node))) == target
    ]
    if not candidates:
        return description_html
    candidates[-1].decompose()
    return root.decode_contents()


def native_job_url(identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"Paytm official-listing job ID is not a UUID: {identity!r}")
    return DETAIL_URL_TEMPLATE.format(job_id=identity)


def apply_url_for(identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"Paytm official-listing job ID is not a UUID: {identity!r}")
    return APPLY_URL_TEMPLATE.format(job_id=identity)


def parse_official_entry(page_html, response_url=ENTRY_URL):
    parsed = urlsplit(str(response_url))
    if parsed.scheme != "https" or parsed.hostname not in ENTRY_HOSTS:
        raise ValueError(f"Paytm official careers page left its HTTPS site: {response_url}")
    if parsed.path.rstrip("/") != "/careers":
        raise ValueError(f"Paytm official careers page has an unexpected path: {response_url}")

    soup = BeautifulSoup(page_html or "", "lxml")
    page_text = soup.get_text(" ", strip=True)
    if "work at paytm" not in page_text.lower():
        raise ValueError("Paytm official careers page no longer carries its careers copy")

    canonical = None
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    if canonical_node is not None:
        canonical = urljoin(str(response_url), canonical_node.get("href", "").strip())
        if canonical.rstrip("/") != ENTRY_CANONICAL:
            raise ValueError(f"Paytm careers canonical changed: {canonical!r}")

    board_links, other_lever_links = [], []
    for anchor in soup.select("a[href]"):
        href = urljoin(str(response_url), anchor.get("href", "").strip())
        target = urlsplit(href)
        if target.hostname != BOARD_HOST:
            continue
        if target.path.strip("/") != BOARD_SLUG:
            other_lever_links.append(href)
            continue
        board_links.append({
            "text": _clean(anchor.get_text(" ", strip=True)),
            "href": href,
            "target": anchor.get("target"),
            "rel": anchor.get("rel"),
        })
    if not board_links:
        raise ValueError("Paytm official careers page no longer links its published Lever board")
    if other_lever_links:
        raise ValueError(f"Paytm official careers page links an unexpected Lever board: {other_lever_links}")

    labels = {link["text"] for link in board_links}
    if not ({"View All Roles", "Apply Now"} & labels):
        raise ValueError("Paytm official careers page lost its role/apply CTA labels")

    return {
        "url": str(response_url),
        "title": _clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "canonical": canonical,
        "board_url": BOARD_URL,
        "board_links": board_links,
        "board_link_count": len(board_links),
        "has_view_all_roles": "View All Roles" in labels,
        "has_apply_now": "Apply Now" in labels,
        "other_lever_links": other_lever_links,
    }


def parse_board(page_html, response_url=BOARD_URL):
    parsed = urlsplit(str(response_url))
    if parsed.scheme != "https" or parsed.hostname != BOARD_HOST:
        raise ValueError(f"Paytm board request left the published Lever host: {response_url}")
    if parsed.path.strip("/") != BOARD_SLUG:
        raise ValueError(f"Paytm board request left the published Lever slug: {response_url}")

    soup = BeautifulSoup(page_html or "", "lxml")
    if soup.select_one(".postings-wrapper") is None:
        raise ValueError("Paytm Lever board response has no postings list container")
    groups = soup.select(".postings-group")
    if not groups:
        raise ValueError("Paytm Lever board response has no posting groups")

    rows, seen = [], set()
    department = None
    for group in groups:
        dept_node = group.select_one(".large-category-header")
        if dept_node is not None:
            department = _clean(dept_node.get_text(" ", strip=True))
        team_node = group.select_one(".posting-category-title")
        team = _clean(team_node.get_text(" ", strip=True)) if team_node is not None else None
        for posting in group.select(".posting"):
            identity = (posting.get("data-qa-posting-id") or "").strip()
            if not UUID.fullmatch(identity):
                raise ValueError(f"Paytm board contains a missing or invalid posting ID: {identity!r}")
            if identity in seen:
                raise ValueError(f"Paytm board repeats a company-published posting ID: {identity}")
            seen.add(identity)

            title_node = posting.select_one('[data-qa="posting-name"]')
            title = _clean(title_node.get_text(" ", strip=True)) if title_node is not None else None
            if not title:
                raise ValueError(f"Paytm board posting {identity} has no title")
            link_node = posting.select_one("a.posting-title[href]")
            if link_node is None:
                raise ValueError(f"Paytm board posting {identity} has no detail link")
            detail_url = urljoin(str(response_url), link_node.get("href", "").strip())
            if detail_url.rstrip("/") != native_job_url(identity):
                raise ValueError(f"Paytm board posting {identity} links an unexpected detail route: {detail_url}")

            workplace = None
            workplace_node = posting.select_one(".posting-categories .workplaceTypes")
            if workplace_node is not None:
                workplace = _clean(workplace_node.get_text(" ", strip=True))
                workplace = _clean(re.sub(r"\s*[—–-]\s*$", "", workplace or ""))
            commitment = None
            commitment_node = posting.select_one(".posting-categories .commitment")
            if commitment_node is not None:
                commitment = _clean(commitment_node.get_text(" ", strip=True))
            location = None
            location_node = posting.select_one(".posting-categories .location")
            if location_node is not None:
                location = _clean(location_node.get_text(" ", strip=True))

            rows.append({
                "id": identity,
                "title": title,
                "url": native_job_url(identity),
                "department": department,
                "team": team,
                "employment_type": commitment,
                "workplace_type": workplace,
                "location": location,
            })
    if not rows:
        raise ValueError("Paytm Lever board contains no visible postings")

    filters = []
    for wrapper in soup.select(".filter-bar .filter-button-wrapper"):
        label_node = wrapper.select_one(".filter-button")
        options = []
        for anchor in wrapper.select(".filter-popup a.category-link"):
            options.append({
                "text": _clean(anchor.get_text(" ", strip=True)),
                "href": anchor.get("href"),
                "selected": "selected" in (anchor.get("class") or []),
            })
        filters.append({
            "label": _clean(label_node.get_text(" ", strip=True)) if label_node is not None else None,
            "options": options,
        })

    pagination = []
    for node in soup.select("a[href], button"):
        text = _clean(node.get_text(" ", strip=True)) or ""
        href = node.get("href") or ""
        if re.fullmatch(r"(?:load more|next|previous|older|newer)", text, re.I) or re.search(r"[?&]page=\d+", href):
            pagination.append({"text": text, "href": href or None})

    return {
        "url": str(response_url),
        "title": _clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "rows": rows,
        "count": len(rows),
        "groups": len(groups),
        "filters": filters,
        "pagination_controls": pagination,
    }


def parse_api_rows(value):
    if not isinstance(value, list) or not value:
        raise ValueError("Paytm Lever JSON listing is not a non-empty array")
    rows, by_id, issues = [], {}, []
    for row in value:
        if not isinstance(row, dict):
            issues.append("paytm_api_row_not_object")
            continue
        identity = str(row.get("id") or "")
        if not UUID.fullmatch(identity):
            issues.append(f"paytm_api_row_missing_uuid:{identity!r}")
            continue
        if identity in by_id:
            issues.append(f"paytm_api_row_duplicate_id:{identity}")
            continue
        title = _clean(row.get("text"))
        if not title:
            issues.append(f"paytm_api_row_missing_title:{identity}")
        if row.get("hostedUrl") != native_job_url(identity):
            issues.append(f"paytm_api_row_hosted_url_changed:{identity}")
        if row.get("applyUrl") != apply_url_for(identity):
            issues.append(f"paytm_api_row_apply_url_changed:{identity}")
        categories = row.get("categories")
        if not isinstance(categories, dict):
            issues.append(f"paytm_api_row_missing_categories:{identity}")
            categories = {}
        all_locations = categories.get("allLocations")
        if not isinstance(all_locations, list) or not all_locations:
            issues.append(f"paytm_api_row_missing_all_locations:{identity}")
        if not isinstance(categories.get("location"), str) or not categories.get("location").strip():
            issues.append(f"paytm_api_row_missing_location:{identity}")
        if not isinstance(categories.get("department"), str) or not categories.get("department").strip():
            issues.append(f"paytm_api_row_missing_department:{identity}")
        if not isinstance(categories.get("team"), str) or not categories.get("team").strip():
            issues.append(f"paytm_api_row_missing_team:{identity}")
        if not isinstance(row.get("createdAt"), int):
            issues.append(f"paytm_api_row_missing_created_at:{identity}")
        description = row.get("description")
        if not isinstance(description, str) or len(normalized_text(description).strip()) < 100:
            issues.append(f"paytm_api_row_description_empty_or_short:{identity}")
        by_id[identity] = row
        rows.append(row)
    if not rows:
        raise ValueError("Paytm Lever JSON listing contained no usable rows")
    return {"rows": rows, "by_id": by_id, "count": len(rows), "issues": issues}


def parse_detail_page(page_html, expected_id, response_url=None, expected_title=None):
    if not UUID.fullmatch(str(expected_id)):
        raise ValueError(f"Paytm detail expected ID is not a UUID: {expected_id!r}")
    if response_url is not None:
        parsed = urlsplit(str(response_url))
        if parsed.scheme != "https" or parsed.hostname != BOARD_HOST:
            raise ValueError(f"Paytm detail request left the published Lever host: {response_url}")
        if parsed.path.strip("/") != f"{BOARD_SLUG}/{expected_id}":
            raise ValueError(f"Paytm detail request left the exact posting route: {response_url}")

    soup = BeautifulSoup(page_html or "", "lxml")
    scripts = soup.select('script[type="application/ld+json"]')
    if len(scripts) != 1:
        raise ValueError(f"Paytm detail {expected_id} must carry exactly one JSON-LD document ({len(scripts)})")
    try:
        data = json.loads(scripts[0].string or scripts[0].get_text())
    except ValueError as exc:
        raise ValueError(f"Paytm detail {expected_id} JSON-LD is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        raise ValueError(f"Paytm detail {expected_id} document is not a JobPosting")

    title = _clean(data.get("title"))
    if not title:
        raise ValueError(f"Paytm detail {expected_id} has no title")
    if expected_title is not None and _clean(expected_title) != title:
        raise ValueError(
            f"Paytm detail {expected_id} title {title!r} differs from listing title {_clean(expected_title)!r}"
        )

    description_html = data.get("description")
    if not isinstance(description_html, str) or len(normalized_text(description_html).strip()) < 100:
        raise ValueError(f"Paytm detail {expected_id} JSON-LD description is empty or short")

    date_posted = data.get("datePosted")
    if date_posted is not None:
        if not isinstance(date_posted, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted.strip()):
            raise ValueError(f"Paytm detail {expected_id} datePosted is not YYYY-MM-DD: {date_posted!r}")
        date_posted = date_posted.strip()
    valid_through = data.get("validThrough")
    if valid_through is not None and not (
        isinstance(valid_through, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_through.strip())
    ):
        raise ValueError(f"Paytm detail {expected_id} validThrough is not YYYY-MM-DD: {valid_through!r}")
    if isinstance(valid_through, str):
        valid_through = valid_through.strip()

    employer = None
    organization = data.get("hiringOrganization")
    if isinstance(organization, dict):
        employer = _clean(organization.get("name"))

    localities = []
    job_location = data.get("jobLocation")
    places = job_location if isinstance(job_location, list) else (
        [job_location] if isinstance(job_location, dict) else []
    )
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if not isinstance(address, dict):
            continue
        locality = _clean(address.get("addressLocality"))
        if locality and locality not in localities:
            localities.append(locality)

    header = soup.select_one(".posting-headline")
    if header is None:
        raise ValueError(f"Paytm detail {expected_id} has no rendered posting header")
    header_title_node = header.select_one("h2")
    header_title = _clean(header_title_node.get_text(" ", strip=True)) if header_title_node is not None else None
    if header_title != title:
        raise ValueError(f"Paytm detail {expected_id} rendered title {header_title!r} differs from JSON-LD {title!r}")

    header_categories = []
    for node in header.select(".posting-category"):
        classes = node.get("class") or []
        kind = next((name for name in ("location", "department", "commitment", "workplaceTypes") if name in classes), None)
        header_categories.append({"kind": kind, "text": _clean(node.get_text(" ", strip=True))})

    apply_hrefs = sorted({
        urljoin(str(response_url or native_job_url(expected_id)), anchor.get("href", "").strip())
        for anchor in soup.select("a.postings-btn[href]")
        if anchor.get("href", "").strip()
    })
    expected_apply = apply_url_for(expected_id)
    if apply_hrefs != [expected_apply]:
        raise ValueError(f"Paytm detail {expected_id} Apply target differs: {apply_hrefs!r} != [{expected_apply!r}]")

    sections, unknown_sections = [], []
    for section in soup.select(".section"):
        data_qa = section.get("data-qa")
        requirements = section.select_one('[data-qa="posting-requirements"]')
        if data_qa in ("job-description", "closing-description", "salary-range") or requirements is not None:
            kind = "requirements" if requirements is not None else data_qa
            heading_node = section.select_one("h3")
            heading = _clean(heading_node.get_text(" ", strip=True)) if heading_node is not None else None
            sections.append({
                "kind": kind,
                "heading": heading,
                "html": section.decode_contents(),
                "text": normalized_text(section.decode_contents()),
            })
        elif data_qa in ("ai-disclaimer", "btn-apply-bottom") or "posting-header" in (section.get("class") or []):
            continue
        elif section.get_text(" ", strip=True):
            heading_node = section.select_one("h3")
            unknown_sections.append({
                "kind": "unknown",
                "data_qa": data_qa,
                "classes": section.get("class"),
                "heading": _clean(heading_node.get_text(" ", strip=True)) if heading_node is not None else None,
                "html": section.decode_contents(),
                "text": normalized_text(section.decode_contents()),
            })

    role_html = "".join(section["html"] for section in sections)
    role_text = normalized_text(role_html)
    description_text = normalized_text(description_html)
    ai_disclaimer = soup.select_one('[data-qa="ai-disclaimer"]')
    ai_text = normalized_text(ai_disclaimer.decode_contents()) if ai_disclaimer is not None else None
    ai_disclaimer_stripped = False
    if ai_text and description_text.rstrip().endswith(ai_text.rstrip()):
        ai_disclaimer_stripped = True
        description_text = description_text.rstrip()[:-len(ai_text.rstrip())].rstrip()
        description_html = _strip_trailing_disclaimer_html(description_html, ai_text)
        if role_text.rstrip().endswith(ai_text.rstrip()):
            role_text = role_text.rstrip()[:-len(ai_text.rstrip())].rstrip()
    description_matches_rendered = compact_body_hash(description_text) == compact_body_hash(role_text)
    salary_text = next((section["text"] for section in sections if section["kind"] == "salary-range"), None)

    return {
        "id": expected_id,
        "url": str(response_url or native_job_url(expected_id)),
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_sha256": compact_body_hash(description_text),
        "rendered_role_html": role_html,
        "rendered_role_text": role_text,
        "rendered_role_sha256": compact_body_hash(role_text),
        "description_matches_rendered": description_matches_rendered,
        "date_posted": date_posted,
        "valid_through": valid_through,
        "employment_type": _clean(data.get("employmentType")),
        "employer": employer,
        "localities": localities,
        "header_categories": header_categories,
        "apply_url": expected_apply,
        "sections": sections,
        "unknown_sections": unknown_sections,
        "salary_text": salary_text,
        "ai_disclaimer_present": ai_disclaimer is not None,
        "ai_disclaimer_text": ai_text,
        "ai_disclaimer_stripped_from_description": ai_disclaimer_stripped,
        "jsonld": data,
    }


def section_roles(sections):
    responsibilities, qualifications, other = [], [], []
    for section in sections:
        if section.get("kind") != "requirements":
            continue
        heading = section.get("heading") or ""
        item = {"heading": heading, "html": section.get("html"), "text": section.get("text")}
        if RESPONSIBILITY_HEADING.search(heading):
            responsibilities.append(item)
        elif QUALIFICATION_HEADING.search(heading):
            qualifications.append(item)
        else:
            other.append(item)
    return responsibilities, qualifications, other


def salary_from_detail(detail, api_row):
    salary_text = detail.get("salary_text") if detail else None
    api_salary = (api_row or {}).get("salaryRange")
    if not salary_text and not isinstance(api_salary, dict):
        return None
    value = {
        "text": salary_text,
        "source": "detail_page_salary_range" if salary_text else "lever_api_salaryRange",
    }
    if isinstance(api_salary, dict):
        for key in ("min", "max", "currency", "interval"):
            if key in api_salary:
                value[key] = api_salary[key]
        if api_salary.get("min") == 0 and api_salary.get("max") == 0:
            value["published_zero_range"] = True
    return value


def _api_all_locations(api_row):
    categories = (api_row or {}).get("categories") or {}
    values = categories.get("allLocations")
    if isinstance(values, list):
        return [_clean(value) for value in values if _clean(value)]
    return []


def _board_location_matches(board_location, locations):
    if not board_location:
        return True
    parts = [_clean(part) for part in re.split(r"\s*/\s*", board_location) if _clean(part)]
    return parts == locations


class Paytm(Adapter):
    """Follow only Paytm's official careers link to its published Lever board."""

    VERIFICATION_REVISION = "paytm.official-careers-lever-board-jsonld.1"

    def _make_job(self, row, api_row, detail, response_meta):
        identity = row["id"]
        title = row["title"]
        api_row = api_row or {}
        categories = api_row.get("categories") or {}
        locations = _api_all_locations(api_row)
        if not locations and row.get("location"):
            locations = [_clean(part) for part in re.split(r"\s*/\s*", row["location"]) if _clean(part)]

        if detail is not None:
            description_html = detail["description_html"]
            description_text = detail["description_text"]
            date_posted = detail["date_posted"]
            valid_through = detail["valid_through"]
            employment_type = detail["employment_type"] or row.get("employment_type")
            employer = detail["employer"]
            apply_url = detail["apply_url"]
            responsibilities, qualifications, other = section_roles(detail["sections"])
            if detail["localities"] and set(detail["localities"]) != set(locations):
                self.result.errors.append(
                    f"{identity}: detail jobLocation {detail['localities']!r} differs from board/API locations {locations!r}"
                )
            if detail["employment_type"] and row.get("employment_type") and detail["employment_type"] != row["employment_type"]:
                self.result.errors.append(
                    f"{identity}: detail employmentType {detail['employment_type']!r} differs from board commitment {row['employment_type']!r}"
                )
            if AI_DISCLAIMER_MARKER in description_text:
                self.result.errors.append(f"{identity}: detail description leaked the generic hiring-AI page chrome")
            if not detail["description_matches_rendered"]:
                self.result.warnings.append(f"jsonld_description_differs_from_rendered_sections:{identity}")
            if detail["unknown_sections"]:
                self.result.warnings.append(f"unknown_detail_sections:{identity}")
        else:
            description_html = ""
            description_text = ""
            date_posted = None
            valid_through = None
            employment_type = row.get("employment_type")
            employer = None
            apply_url = apply_url_for(identity)
            responsibilities, qualifications, other = [], [], []

        salary = salary_from_detail(detail, api_row)
        raw = {
            "listing": row,
            "api": api_row,
            "hosted_url": native_job_url(identity),
            "apply_url": apply_url,
            "country": api_row.get("country"),
            "workplace_type_raw": api_row.get("workplaceType"),
            "all_locations": locations,
            "lever_createdAt": api_row.get("createdAt"),
            "date_posted_source": "detail_jsonld_datePosted" if date_posted else None,
            "responsibility_sections": responsibilities,
            "qualification_sections": qualifications,
            "other_requirement_sections": other,
        }
        if detail is not None:
            raw["detail"] = {
                "jsonld": detail["jsonld"],
                "header_categories": detail["header_categories"],
                "sections": detail["sections"],
                "unknown_sections": detail["unknown_sections"],
                "salary_text": detail["salary_text"],
                "description_matches_rendered": detail["description_matches_rendered"],
                "description_sha256": detail["description_sha256"],
                "rendered_role_sha256": detail["rendered_role_sha256"],
                "response_sha256": response_meta.get("sha256"),
                "response_bytes": response_meta.get("bytes"),
                "final_url": response_meta.get("final_url"),
                "ai_disclaimer_present": detail["ai_disclaimer_present"],
                "ai_disclaimer_stripped_from_description": detail["ai_disclaimer_stripped_from_description"],
            }

        job = self.job(
            raw,
            title=title,
            url=native_job_url(identity),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="paytm.official_careers_lever_detail.jsonld_description",
            description_url=native_job_url(identity),
            locations=locations,
            department=categories.get("department") or row.get("department"),
            team=categories.get("team") or row.get("team"),
            employment_type=employment_type,
            workplace_type=row.get("workplace_type"),
            date_posted=date_posted,
            date_posted_raw=date_posted,
            date_updated=None,
            valid_through=valid_through,
            salary=salary,
            qualifications=[item["text"] for item in qualifications] or None,
            responsibilities=[item["text"] for item in responsibilities] or None,
            employer=employer,
        )
        job.description_text = description_text
        job.record_identity = f"paytm:{identity}"
        if detail is None:
            job.warnings.append("detail_fetch_failed")
        if not _board_location_matches(row.get("location"), locations):
            job.warnings.append("board_location_differs_from_api_all_locations")
        return job

    async def _detail_snapshot(self, job):
        response = await self.http.get(job.url)
        final_url = str(getattr(response, "url", job.url))
        if final_url.rstrip("/") != job.url.rstrip("/"):
            raise ValueError(f"Paytm exact detail page {job.url} redirected to an uninspected route: {final_url}")
        detail = parse_detail_page(
            response.text,
            job.job_id,
            response_url=final_url,
            expected_title=job.title,
        )
        digest = sha256(response.content).hexdigest()
        return self._make_job(
            job.raw["listing"],
            job.raw.get("api") or {},
            detail,
            {"sha256": digest, "bytes": len(response.content), "final_url": final_url},
        )

    async def run(self):
        started = time.perf_counter()
        if not self.page_guard():
            return self.result

        entry_started = time.perf_counter()
        entry = await self.http.get(ENTRY_URL)
        entry_seconds = time.perf_counter() - entry_started
        entry_info = parse_official_entry(entry.text, str(entry.url))

        board_started = time.perf_counter()
        board_response = await self.http.get(BOARD_URL)
        board_seconds = time.perf_counter() - board_started
        board = parse_board(board_response.text, str(board_response.url))

        api_started = time.perf_counter()
        api_response = await self.http.get(BOARD_API_URL)
        api_seconds = time.perf_counter() - api_started
        try:
            api_value = json.loads(api_response.content)
        except ValueError as exc:
            raise ValueError("Paytm Lever JSON listing response is not valid JSON") from exc
        api = parse_api_rows(api_value)

        board_ids = [row["id"] for row in board["rows"]]
        api_ids = [row["id"] for row in api["rows"]]
        if set(board_ids) != set(api_ids):
            self.result.errors.append(
                "paytm_board_api_membership_mismatch:"
                f"board_only={sorted(set(board_ids) - set(api_ids))},"
                f"api_only={sorted(set(api_ids) - set(board_ids))}"
            )
        self.result.errors.extend(api["issues"])

        jobs = [
            self._make_job(row, api["by_id"].get(row["id"]), None, {})
            for row in board["rows"]
        ]
        self.result.expected_total = len(board["rows"])
        self.result.pages = 1
        self.result.listing_complete = not self.result.errors

        await self.hydrate(jobs, self._detail_snapshot)
        for job in jobs:
            self.add(job)

        final_started = time.perf_counter()
        final_response = await self.http.get(BOARD_URL)
        final_seconds = time.perf_counter() - final_started
        final = parse_board(final_response.text, str(final_response.url))
        final_ids = [row["id"] for row in final["rows"]]
        final_snapshot_verified = True
        if set(final_ids) != set(board_ids):
            final_snapshot_verified = False
            self.result.errors.append(
                "paytm_official_listing_membership_changed_between_initial_and_final_snapshots:"
                f"initial_only={sorted(set(board_ids) - set(final_ids))},"
                f"final_only={sorted(set(final_ids) - set(board_ids))}"
            )
        self.result.expected_total = len(final["rows"])
        self.result.listing_complete = (
            final_snapshot_verified
            and set(final_ids) == set(board_ids) == set(api_ids)
            and not self.result.errors
        )

        dates = sorted(job.date_posted for job in self.result.jobs if job.date_posted)
        salary_jobs = [job.job_id for job in self.result.jobs if job.salary]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "entry_status": entry.status_code,
            "entry_final_url": str(entry.url),
            "entry_title": entry_info["title"],
            "entry_canonical": entry_info["canonical"],
            "entry_board_links": entry_info["board_links"],
            "entry_board_link_count": entry_info["board_link_count"],
            "entry_has_view_all_roles": entry_info["has_view_all_roles"],
            "entry_has_apply_now": entry_info["has_apply_now"],
            "board_url": BOARD_URL,
            "board_status": board_response.status_code,
            "board_title": board["title"],
            "board_count": board["count"],
            "board_groups": board["groups"],
            "board_filters": board["filters"],
            "board_pagination_controls": board["pagination_controls"],
            "api_url": BOARD_API_URL,
            "api_status": api_response.status_code,
            "api_count": api["count"],
            "api_ids": api_ids,
            "api_issues": api["issues"],
            "initial_visible_ids": board_ids,
            "final_visible_ids": final_ids,
            "final_snapshot_verified": final_snapshot_verified,
            "membership_authority": (
                "Paytm's own https://paytm.com/careers/ page publishes Apply Now / View All Roles "
                "anchors to exactly https://jobs.lever.co/paytm. The unfiltered board page renders "
                "every open card; filters are query-string subsets and there is no pagination."
            ),
            "listing_filter_behavior": (
                "Location type, Location, Team and Work type filter popups link to query-string "
                "subsets of the same board. The unfiltered page is the full membership."
            ),
            "ui_pagination": (
                "None. The single unfiltered board page renders all visible cards grouped by "
                "department/team headers; no paged request, cursor or Load more control was observed."
            ),
            "detail_route_template": DETAIL_URL_TEMPLATE,
            "apply_route_template": APPLY_URL_TEMPLATE,
            "description_source": (
                "Each exact listed detail page's JobPosting JSON-LD description HTML; its text was "
                "reconciled with the rendered job-description, posting-requirements, "
                "closing-description and salary-range sections."
            ),
            "ai_disclaimer_excluded": True,
            "date_posted_source": "detail_jsonld_datePosted",
            "date_posted_min": dates[0] if dates else None,
            "date_posted_max": dates[-1] if dates else None,
            "salary_jobs": salary_jobs,
            "ats_wide_search_or_enumeration_used": False,
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_board_requests": 2,
            "board_json_reconciliation_request": 1,
        })
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["board_listing_phase_seconds"] = round(board_seconds, 6)
        self.result.timings["api_reconciliation_phase_seconds"] = round(api_seconds, 6)
        self.result.timings["final_board_recheck_seconds"] = round(final_seconds, 6)
        return self.result
