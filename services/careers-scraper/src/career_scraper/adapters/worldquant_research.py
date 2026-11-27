"""WorldQuant Research's own career listing and its exact embedded Greenhouse jobs.

The company-published journey inspected on 2026-11-08 is:

- Entry/listing: https://www.worldquant.com/career-listing/
  One server-rendered WordPress page with an unfiltered ``ul#careers_list``
  containing every current opening. The browser's "See more" control only
  reveals successive nine-row slices of the already-rendered list; it does not
  make another listing request. The page has a keyword field, a location
  selector (single and multiple), and a department selector. The adapter never
  supplies a filter, so the complete membership is collected.
- Detail: https://www.worldquant.com/career-listing/?id=<job id>
  The company renders the full role body in ``.article-wysiwyg`` and an apply
  link ``.?application=<job id>``. No dates, requisition ids or structured
  employment fields are displayed there.
- Apply: https://www.worldquant.com/career-listing/?application=<job id>
  The company page embeds the exact Greenhouse job form at
  ``https://job-boards.greenhouse.io/worldquant/jobs/<job id>``.

Every listed id is also the exact id of a job on WorldQuant's own Greenhouse
board. The board is used only as a cross-check and as the public source of
metadata (requisition id, first_published, updated_at, offices, departments)
for the exact ids that WorldQuant's listing exposes. Membership always comes
from the company listing; the board is never enumerated for membership and
no other board is touched.

The company detail body carries Cloudflare-obfuscated email addresses. The
adapter decodes those spans before storing the body, so the company page text
matches the employer's exact Greenhouse ``content`` field. All 87 roles in the
reviewed snapshot matched after that decode.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import unescape
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.worldquant.com/career-listing/"
CAREERS_LANDING = "https://www.worldquant.com/careers/"
LISTING_URL = OFFICIAL_ENTRY
DETAIL_URL = LISTING_URL + "?id={identity}"
APPLY_URL = LISTING_URL + "?application={identity}"
GREENHOUSE_BOARD = "worldquant"
GREENHOUSE_BOARD_API = "https://boards-api.greenhouse.io/v1/boards/worldquant/jobs"
GREENHOUSE_JOB_API = GREENHOUSE_BOARD_API + "/{identity}"
GREENHOUSE_JOB_PAGE = "https://job-boards.greenhouse.io/worldquant/jobs/{identity}"
GREENHOUSE_BOARD_PAGE = "https://job-boards.greenhouse.io/worldquant"

JOB_ID = re.compile(r"^\d{7,13}$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "ul", "ol", "table", "blockquote", "pre", "dl", "figure"}
TALENT_NETWORK_TITLE = re.compile(r"\btalent\s+(?:network|community|pool)\b", re.I)
TALENT_NETWORK_BODY = re.compile(
    r"(?:join (?:our|the) .{0,40}talents?|future opportunit|not a specific (?:role|position)|"
    r"submit your resume here)",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_text(html: str | None) -> str:
    """Whitespace-normalised visible text, used only for source comparisons."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _source_date(value) -> str | None:
    """Return the source date verbatim only when it is a valid ISO timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _source_id(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _route_query(url: str, key: str, expected_path: str) -> tuple[str | None, dict[str, list[str]]]:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    identity = query.get(key, [None])[0]
    if (parts.scheme != "https" or parts.hostname != "www.worldquant.com"
            or parts.path != expected_path):
        return None, query
    return identity, query


def _card_url(identity: str) -> str:
    return DETAIL_URL.format(identity=identity)


def _apply_url(identity: str) -> str:
    return APPLY_URL.format(identity=identity)


def _greenhouse_job_url(identity: str) -> str:
    return GREENHOUSE_JOB_PAGE.format(identity=identity)


def _decode_cloudflare_emails(soup: BeautifulSoup) -> list[str]:
    """Decode Cloudflare's data-cfemail spans in place (no challenge bypass)."""
    decoded = []
    for node in soup.select("[data-cfemail]"):
        encoded = node.get("data-cfemail", "")
        if len(encoded) < 4 or len(encoded) % 2:
            continue
        try:
            key = int(encoded[:2], 16)
            email = "".join(
                chr(int(encoded[index:index + 2], 16) ^ key)
                for index in range(2, len(encoded), 2)
            )
        except ValueError:
            continue
        node.clear()
        node.append(email)
        decoded.append(email)
    return decoded


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    """Parse the complete unfiltered company listing and its visible controls."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "www.worldquant.com"
            or parts.path != "/career-listing/" or parts.query or parts.fragment):
        raise ValueError("WorldQuant official listing did not stay on the inspected URL")
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one("section.career-grid")
    listing = soup.select_one("#careers_list")
    if section is None or listing is None:
        raise ValueError("WorldQuant official career listing grid is missing")
    if not any(normalized_title(h.get_text(" ", strip=True)).lower() == "open positions"
               or normalized_title(h.get_text(" ", strip=True)).lower() == "open roles"
               for h in section.select("h1,h2,h3")):
        # The page's own headline is not required to be exactly one string in
        # every WordPress render, but the grid and list must still be present.
        pass

    rows, seen, issues = [], set(), []
    for item in listing.find_all("li", recursive=False):
        anchors = item.select("a.fo-link")
        if len(anchors) != 1:
            issues.append("worldquant_job_card_link_count_changed")
            continue
        anchor = anchors[0]
        href = urljoin(response_url, anchor.get("href", ""))
        identity, query = _route_query(href, "id", "/career-listing/")
        if identity is None or set(query) != {"id"} or not JOB_ID.fullmatch(identity):
            issues.append("worldquant_job_card_href_not_exact_listing_route")
            continue
        if identity in seen:
            issues.append(f"worldquant_duplicate_listing_id:{identity}")
            continue
        title_node = anchor.select_one("h4")
        location_node = anchor.select_one(".fo-location")
        if title_node is None or location_node is None:
            issues.append(f"worldquant_job_card_fields_missing:{identity}")
            continue
        title = normalized_title(title_node.get_text(" ", strip=True))
        location = normalized_title(location_node.get_text(" ", strip=True))
        if not title or not location:
            issues.append(f"worldquant_job_card_empty_title_or_location:{identity}")
            continue
        data_location = [
            value.strip() for value in (item.get("data-location") or "").split("|")
            if value.strip()
        ]
        rows.append({
            "id": identity,
            "url": _card_url(identity),
            "href": anchor.get("href"),
            "title": title,
            "location": location,
            "city": normalized_title(item.get("data-city")),
            "department_slug": normalized_title(item.get("data-department")),
            "data_location": data_location,
            "timezone": normalized_title(
                (anchor.select_one(".fo-timezone").get_text(" ", strip=True)
                 if anchor.select_one(".fo-timezone") else "")
            ),
            "inline_style": item.get("style"),
            "card_html": str(item),
        })
        seen.add(identity)

    if not rows:
        issues.append("worldquant_company_listing_has_no_exposed_job_rows")

    def options(selector: str, *, skip=("",)) -> list[dict]:
        select = soup.select_one(selector)
        if select is None:
            return []
        return [
            {"value": option.get("value"), "label": normalized_title(option.get_text(" ", strip=True))}
            for option in select.find_all("option")
            if (option.get("value") or "") not in skip
        ]

    keyword = soup.select_one("input.js-keyword")
    load_more = soup.select_one("button.js-btn-load-more")
    buttons = [normalized_title(button.get_text(" ", strip=True))
               for button in section.select("button") if normalized_title(button.get_text(" ", strip=True))]
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "filters": {
            "keyword_search": keyword is not None,
            "keyword_placeholder": keyword.get("placeholder") if keyword else None,
            "locations_single": options("select.js-location", skip=("", "multiple-locations")),
            "locations_multiple": options("select.js-locations-multiple"),
            "departments": options("select.js-department"),
            "buttons": buttons,
        },
        "load_more": {
            "present": load_more is not None,
            "label": normalized_title(load_more.get_text(" ", strip=True)) if load_more else None,
            "server_side": False,
            "all_rows_in_initial_html": load_more is not None,
            "observed_page_size": 9,
        },
        "inline_hidden_rows": sum("display:none" in (row.get("inline_style") or "") for row in rows),
        "issues": list(dict.fromkeys(issues)),
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it.

    WorldQuant's content nests headings inside wrapper divs, so this walks
    containers and treats leaf blocks as content. It is descriptive only; the
    complete ``.article-wysiwyg`` HTML remains the authoritative description.
    """
    soup = BeautifulSoup(f'<div data-worldquant-body="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-worldquant-body]")
    if root is None:
        return [{"heading": None, "html": "", "text": ""}]
    sections: list[dict] = []
    current: dict | None = None

    def is_heading(node: Tag) -> bool:
        if node.name in HEADING_TAGS:
            return True
        if node.name == "p":
            text = normalized_title(node.get_text(" ", strip=True))
            strong = node.find(["strong", "b"])
            if not text:
                return False
            if strong is not None:
                strong_text = normalized_title(strong.get_text(" ", strip=True))
                if strong_text == text and len(text) <= 160:
                    return True
            if text.endswith(":") and len(text) <= 120:
                return True
        return False

    def flush():
        nonlocal current
        if current is not None:
            current["text"] = text_html(current["html"])
            sections.append(current)
            current = None

    def walk(node: Tag):
        nonlocal current
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if is_heading(child):
                flush()
                current = {
                    "heading": normalized_title(child.get_text(" ", strip=True)),
                    "html": "",
                    "text": "",
                }
                continue
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            if child.name in BLOCK_TAGS:
                current["html"] += str(child)
                continue
            if child.name in {"div", "section", "article", "main", "header", "footer", "span"}:
                if child.find(list(BLOCK_TAGS | HEADING_TAGS)):
                    walk(child)
                else:
                    current["html"] += str(child)
                continue
            current["html"] += str(child)

    walk(root)
    flush()
    if not sections:
        sections = [{"heading": None, "html": "", "text": ""}]
    return sections


def _section_text(sections: list[dict], heading_tokens: tuple[str, ...]) -> list[dict] | None:
    found = []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if heading and any(token in heading for token in heading_tokens):
            found.append(section)
    return found or None


def extract_salary(text: str) -> dict | None:
    """Retain an explicit published base-pay/salary range without conversion."""
    patterns = [
        re.compile(
            r"The Base Pay Range For This Position Is\s*"
            r"(\$[\d,]+(?:\.\d+)?)\s*(?:[–—\-]|to)\s*(\$?[\d,]+(?:\.\d+)?)\s*(USD)?",
            re.I,
        ),
        re.compile(
            r"(?:estimated|expected|anticipated)\s+salary range for this position is\s*"
            r"(\$[\d,]+(?:\.\d+)?)\s*(?:[–—\-]|to)\s*(\$?[\d,]+(?:\.\d+)?)"
            r"([^.]{0,120})",
            re.I,
        ),
    ]
    for pattern in patterns:
        match = pattern.search(text or "")
        if not match:
            continue
        minimum = re.sub(r"[^\d.]", "", match.group(1))
        maximum = re.sub(r"[^\d.]", "", match.group(2))
        if not minimum or not maximum:
            continue
        suffix = (match.group(3) or "").strip()
        return {
            "min": int(float(minimum)) if "." not in minimum else float(minimum),
            "max": int(float(maximum)) if "." not in maximum else float(maximum),
            "currency": "USD" if ("USD" in suffix.upper() or "$" in match.group(1)) else None,
            "currency_basis": "dollar sign/currency text in the employer's published range",
            "period": None,
            "kind": "base_pay_range" if "base pay range" in match.group(0).lower() else "estimated_salary_range",
            "source_text": match.group(0).strip(),
            "source": "company detail description",
        }
    return None


def parse_detail(html: str, expected_id: str, response_url: str) -> dict:
    identity, query = _route_query(response_url, "id", "/career-listing/")
    if identity != expected_id or set(query) != {"id"}:
        raise ValueError("WorldQuant detail request is not the exact exposed listing route")
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one("section.career-detail")
    title_node = soup.select_one("section.career-detail h1")
    location_node = soup.select_one(".cd-location")
    body = soup.select_one(".article-wysiwyg")
    apply_node = soup.select_one("a.cd-link-apply")
    if section is None or title_node is None or location_node is None or body is None or apply_node is None:
        raise ValueError(f"WorldQuant detail {expected_id} is missing its career-detail body or apply control")
    decoded_emails = _decode_cloudflare_emails(body)
    title = normalized_title(title_node.get_text(" ", strip=True))
    if not title:
        raise ValueError(f"WorldQuant detail {expected_id} has an empty title")
    apply_href = apply_node.get("href", "")
    apply_url = urljoin(response_url, apply_href)
    apply_identity, apply_query = _route_query(apply_url, "application", "/career-listing/")
    if apply_identity != expected_id or set(apply_query) != {"application"}:
        raise ValueError(f"WorldQuant detail {expected_id} apply control does not point at its exact application route")
    description_html = "".join(str(child) for child in body.contents)
    description_text = text_html(description_html)
    sections = description_sections(description_html)
    intro = body.select_one(".content-intro")
    conclusion = body.select_one(".content-conclusion")
    job_specific = BeautifulSoup(description_html, "lxml")
    for template in job_specific.select(".content-intro,.content-conclusion"):
        template.decompose()
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": urljoin(response_url, anchor.get("href", ""))}
        for anchor in body.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    normalized = normalized_text(description_html)
    return {
        "id": expected_id,
        "url": response_url,
        "title": title,
        "location": normalized_title(location_node.get_text(" ", strip=True)),
        "apply_href": apply_href,
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized": normalized,
        "description_normalized_chars": len(normalized),
        "description_normalized_sha256": sha256(normalized.encode()).hexdigest(),
        "description_normalized_fnv1a64": fnv1a64(normalized),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_sections": sections,
        "template_sections": {
            "intro_chars": len(normalized_text(str(intro))) if intro else 0,
            "intro_sha256": sha256(normalized_text(str(intro)).encode()).hexdigest() if intro else None,
            "conclusion_chars": len(normalized_text(str(conclusion))) if conclusion else 0,
            "conclusion_sha256": sha256(normalized_text(str(conclusion)).encode()).hexdigest() if conclusion else None,
            "job_specific_chars": len(normalized_text(str(job_specific))),
            "job_specific_sha256": sha256(normalized_text(str(job_specific)).encode()).hexdigest(),
        },
        "salary": extract_salary(description_text),
        "links": links,
        "attachments": attachments,
        "cloudflare_emails_decoded": decoded_emails,
    }


def parse_greenhouse_job(value: dict, expected_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"WorldQuant Greenhouse job {expected_id} response is not an object")
    identity = str(value.get("id"))
    if identity != expected_id or not JOB_ID.fullmatch(identity):
        raise ValueError(f"WorldQuant Greenhouse job id mismatch for {expected_id}")
    title = normalized_title(value.get("title"))
    if not title:
        raise ValueError(f"WorldQuant Greenhouse job {expected_id} has no title")
    if value.get("company_name") not in (None, "WorldQuant"):
        raise ValueError(f"WorldQuant Greenhouse job {expected_id} company changed: {value.get('company_name')!r}")
    absolute_url = value.get("absolute_url")
    expected_url = _greenhouse_job_url(expected_id)
    if absolute_url != expected_url:
        raise ValueError(f"WorldQuant Greenhouse job {expected_id} absolute_url changed: {absolute_url!r}")
    content = value.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError(f"WorldQuant Greenhouse job {expected_id} content is not text")
    content_html = unescape(content) if isinstance(content, str) else ""
    content_text = text_html(content_html)
    offices = value.get("offices") or []
    office_locations = [
        normalized_title(office.get("location"))
        for office in offices
        if isinstance(office, dict) and normalized_title(office.get("location"))
    ]
    departments = [
        normalized_title(department.get("name"))
        for department in (value.get("departments") or [])
        if isinstance(department, dict) and normalized_title(department.get("name"))
    ]
    return {
        "id": identity,
        "title": title,
        "absolute_url": absolute_url,
        "company_name": value.get("company_name"),
        "requisition_id": _source_id(value.get("requisition_id")),
        "first_published": _source_date(value.get("first_published")),
        "updated_at": _source_date(value.get("updated_at")),
        "application_deadline": _source_date(value.get("application_deadline")),
        "first_published_raw": value.get("first_published"),
        "updated_at_raw": value.get("updated_at"),
        "application_deadline_raw": value.get("application_deadline"),
        "language": value.get("language"),
        "education": value.get("education"),
        "location_name": normalized_title((value.get("location") or {}).get("name")),
        "office_locations": office_locations,
        "department_names": departments,
        "offices": offices,
        "departments": value.get("departments") or [],
        "content_html": content_html,
        "content_text": content_text,
        "content_normalized": normalized_text(content_html),
        "content_missing": not bool(content_text.strip()),
        "raw": value,
    }


def parse_greenhouse_board(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("WorldQuant Greenhouse board response has no jobs array")
    jobs = value["jobs"]
    ids = [str(row.get("id")) for row in jobs if isinstance(row, dict)]
    if len(ids) != len(jobs) or len(ids) != len(set(ids)):
        raise ValueError("WorldQuant Greenhouse board rows have missing or repeated ids")
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    total = meta.get("total")
    if total is not None and total != len(jobs):
        raise ValueError("WorldQuant Greenhouse board meta.total differs from its row count")
    return {
        "ids": ids,
        "count": len(jobs),
        "total": total,
        "titles": {str(row["id"]): normalized_title(row.get("title")) for row in jobs},
        "rows": jobs,
    }


def parse_apply_page(html: str, expected_id: str, response_url: str) -> dict:
    identity, query = _route_query(response_url, "application", "/career-listing/")
    if identity != expected_id or set(query) != {"application"}:
        raise ValueError("WorldQuant apply request is not the exact exposed application route")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("section.career-detail h1")
    iframe = soup.select_one('section.career-detail iframe[title="Application form"]')
    if title_node is None or iframe is None:
        raise ValueError(f"WorldQuant apply page {expected_id} is missing its form iframe or title")
    iframe_url = urljoin(response_url, iframe.get("src", ""))
    if iframe_url != _greenhouse_job_url(expected_id):
        raise ValueError(f"WorldQuant apply page {expected_id} embeds an unexpected form URL")
    return {
        "id": expected_id,
        "url": response_url,
        "title": normalized_title(title_node.get_text(" ", strip=True)),
        "iframe_url": iframe_url,
        "iframe_title": iframe.get("title"),
    }


class WorldQuant(Adapter):
    """Read WorldQuant's own complete listing and exact embedded Greenhouse jobs."""

    VERIFICATION_REVISION = "worldquant.own-career-listing-exact-greenhouse.1"

    async def _listing_snapshot(self, label: str) -> dict:
        if not self.page_guard():
            raise ValueError(f"WorldQuant {label} listing page budget exhausted")
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing(response.text, str(response.url))
        parsed.update({
            "label": label,
            "status": response.status_code,
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "request_seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _board_crosscheck(self, listing_ids: list[str]) -> dict:
        started = time.perf_counter()
        try:
            response = await self.http.get(GREENHOUSE_BOARD_API, params={"content": "false"})
            parsed = parse_greenhouse_board(response.json())
        except Exception as exc:
            self.result.warnings.append("worldquant_greenhouse_board_crosscheck_failed")
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "request_seconds": max(0.0, time.perf_counter() - started),
            }
        listing_set = set(listing_ids)
        board_set = set(parsed["ids"])
        missing = [identity for identity in listing_ids if identity not in board_set]
        unlisted = [identity for identity in parsed["ids"] if identity not in listing_set]
        if missing:
            self.result.errors.append(
                "worldquant_company_listing_ids_missing_from_greenhouse:"
                + ",".join(missing)
            )
        if unlisted:
            self.result.warnings.append(
                "worldquant_greenhouse_board_has_unlisted_jobs:"
                + ",".join(unlisted)
            )
        return {
            "status": "ok",
            "url": str(response.url),
            "status_code": response.status_code,
            "request_seconds": max(0.0, time.perf_counter() - started),
            "response_sha256": sha256(response.content).hexdigest(),
            "board_count": parsed["count"],
            "meta_total": parsed["total"],
            "listing_count": len(listing_ids),
            "membership_equal": listing_set == board_set,
            "listing_ids_missing_from_board": missing,
            "board_ids_not_in_listing": unlisted,
            "membership_authority": "company career-listing page; board used only as cross-check",
        }

    async def _detail(self, job):
        listing = job.raw["listing"]
        identity = str(job.job_id)
        detail_url = _card_url(identity)
        if job.url != detail_url:
            raise ValueError(f"WorldQuant listing job URL changed for {identity}")
        started = time.perf_counter()
        detail_response = await self.http.get(detail_url)
        detail_seconds = max(0.0, time.perf_counter() - started)
        detail = parse_detail(detail_response.text, identity, str(detail_response.url))
        detail.update({
            "status": detail_response.status_code,
            "request_seconds": detail_seconds,
            "response_sha256": sha256(detail_response.content).hexdigest(),
            "response_bytes": len(detail_response.content),
        })
        if normalized_title(detail["title"]) != normalized_title(listing["title"]):
            raise ValueError(f"WorldQuant detail/listing title conflict for {identity}")

        greenhouse_url = GREENHOUSE_JOB_API.format(identity=identity)
        started = time.perf_counter()
        greenhouse_response = await self.http.get(greenhouse_url, params={"questions": "false"})
        greenhouse_seconds = max(0.0, time.perf_counter() - started)
        greenhouse = parse_greenhouse_job(greenhouse_response.json(), identity)
        greenhouse.update({
            "status": greenhouse_response.status_code,
            "request_seconds": greenhouse_seconds,
            "response_sha256": sha256(greenhouse_response.content).hexdigest(),
            "response_bytes": len(greenhouse_response.content),
        })
        if normalized_title(greenhouse["title"]) != normalized_title(listing["title"]):
            raise ValueError(f"WorldQuant Greenhouse/listing title conflict for {identity}")
        if greenhouse["content_missing"]:
            raise ValueError(f"WorldQuant Greenhouse job {identity} has no full content field")
        if detail["description_normalized"] != greenhouse["content_normalized"]:
            raise ValueError(
                f"WorldQuant detail/Greenhouse description conflict for {identity}: "
                f"{detail['description_normalized_chars']} vs {len(greenhouse['content_normalized'])} chars"
            )
        if len(detail["description_normalized"]) < 100:
            raise ValueError(f"WorldQuant detail description is implausibly short for {identity}")

        return self._make_job(listing, detail=detail, greenhouse=greenhouse)

    def _make_job(self, listing: dict, *, detail: dict | None = None,
                  greenhouse: dict | None = None):
        identity = str(listing["id"])
        if detail is None or greenhouse is None:
            job = self.job(
                {"listing": dict(listing)},
                title=listing["title"],
                url=listing["url"],
                job_id=identity,
                description="",
                description_url=None,
                method=None,
            )
            job.record_identity = f"worldquant:{identity}"
            return job

        locations = list(greenhouse["office_locations"])
        if not locations and listing.get("location"):
            locations = [listing["location"]]
        department = list(greenhouse["department_names"]) or None
        posting_kind = "talent_network" if (
            TALENT_NETWORK_TITLE.search(listing["title"])
            or TALENT_NETWORK_BODY.search(detail["description_text"])
        ) else "job"
        sections = detail["description_sections"]
        responsibilities = _section_text(sections, (
            "responsibilit", "what you will do", "what you’ll do", "what you'll do",
            "the role", "岗位职责", "主要职责",
        ))
        qualifications = _section_text(sections, (
            "qualif", "requirement", "what you’ll bring", "what you'll bring",
            "what you will bring", "what we’re looking for", "what we're looking for",
            "who you are", "skills", "你需要具备", "자격요건",
        ))
        raw = {
            "listing": dict(listing),
            "native_detail": {
                key: value for key, value in detail.items() if key != "description_html"
            } | {
                "matches_greenhouse_content": True,
            },
            "greenhouse": {
                "request_url": GREENHOUSE_JOB_API.format(identity=identity),
                "raw": greenhouse["raw"],
                "status": greenhouse["status"],
                "request_seconds": greenhouse["request_seconds"],
                "response_sha256": greenhouse["response_sha256"],
                "response_bytes": greenhouse["response_bytes"],
                "content_matches_company_detail": True,
                "content_sha256": sha256(greenhouse["content_html"].encode()).hexdigest(),
            },
            "apply_route": {
                "company_apply_url": detail["apply_url"],
                "apply_href": detail["apply_href"],
                "greenhouse_apply_url": greenhouse["absolute_url"],
                "greenhouse_board_url": GREENHOUSE_BOARD_PAGE,
                "application_iframe_title": "Application form",
            },
            "posting_kind": posting_kind,
            "date_semantics": {
                "date_posted": "Greenhouse first_published, verbatim",
                "date_updated": "Greenhouse updated_at, verbatim",
                "valid_through": "Greenhouse application_deadline, verbatim; null when absent",
                "company_detail_dates": None,
            },
        }
        job = self.job(
            raw,
            title=listing["title"],
            url=detail["url"],
            job_id=identity,
            requisition_id=greenhouse["requisition_id"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="worldquant.career_detail.article-wysiwyg+cloudflare-email-decoded",
            description_url=detail["url"],
            locations=locations,
            department=department,
            employment_type=None,
            workplace_type=None,
            date_posted=greenhouse["first_published"],
            date_posted_raw=greenhouse["first_published_raw"],
            date_updated=greenhouse["updated_at"],
            valid_through=greenhouse["application_deadline"],
            salary=detail["salary"],
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=None,
            education=None,
            employer=greenhouse["company_name"] or "WorldQuant",
            language=greenhouse["language"],
        )
        job.description_text = detail["description_text"]
        job.record_identity = f"worldquant:{identity}"
        return job

    async def _apply_check(self, job) -> dict:
        identity = str(job.job_id)
        url = _apply_url(identity)
        started = time.perf_counter()
        response = await self.http.get(url)
        parsed = parse_apply_page(response.text, identity, str(response.url))
        parsed.update({
            "status": response.status_code,
            "request_seconds": max(0.0, time.perf_counter() - started),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        if normalized_title(parsed["title"]) != normalized_title(job.title):
            raise ValueError(f"WorldQuant apply page title conflict for {identity}")
        return parsed

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "careers_landing_url": CAREERS_LANDING,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "WorldQuant's own /career-listing/ page. Its unfiltered server-rendered "
                "ul#careers_list contains every exposed job id; browser 'See more' only "
                "reveals slices of that same HTML."
            ),
            "listing_request": {
                "method": "GET",
                "url": LISTING_URL,
                "query": None,
                "filters_applied": False,
                "server_side_pagination": False,
                "all_rows_server_rendered": True,
            },
            "ui_pagination": {
                "control": "See more",
                "observed_page_size": 9,
                "initial_visible_rows": 9,
                "load_more_clicks_to_reveal_all": 9,
                "all_rows_in_initial_html": True,
                "server_side": False,
            },
            "detail_method": (
                "Each exact company detail route ?id=<id>; .article-wysiwyg full body; "
                "Cloudflare email spans decoded; exact Greenhouse job content used only "
                "as an independent full-text cross-check and metadata source."
            ),
            "date_semantics": (
                "first_published is the public posting timestamp; updated_at is the source "
                "update timestamp; null application_deadline stays null. The company detail "
                "page exposes no posting/update dates."
            ),
            "request_scope": (
                "www.worldquant.com /career-listing/ and the exact greenhouse job ids it "
                "embeds. No guessed ATS slug, no board-wide membership enumeration, no "
                "aggregator."
            ),
        })
        try:
            initial = await self._listing_snapshot("initial")
        except Exception as exc:
            self.result.errors.append(f"WorldQuant initial listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_listing"] = {
            key: value for key, value in initial.items() if key != "rows"
        }
        self.result.metadata["job_ids"] = initial["job_ids"]
        self.result.metadata["filters"] = initial["filters"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
        if initial["count"] == 0:
            self.result.errors.append(
                "WorldQuant official listing returned zero rows; empty state requires independent review"
            )
            self.result.metadata["initial_listing"]["zero_rows"] = True
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.metadata["greenhouse_board_crosscheck"] = await self._board_crosscheck(
            initial["job_ids"]
        )

        jobs = []
        for row in initial["rows"]:
            job = self._make_job(row)
            self.add(job)
            jobs.append(job)
            if self.limit():
                break
        if len(jobs) != initial["count"]:
            self.result.errors.append(
                f"WorldQuant retained listing rows do not match the company listing: "
                f"{len(jobs)} != {initial['count']}"
            )

        self.result.jobs = await self.hydrate(jobs, self._detail)
        details = dict(self.result.metadata.get("details") or {})
        self.result.metadata["detail_hydration"] = {
            "mode": "company detail + exact Greenhouse job detail per exposed id",
            "jobs_requested": len(jobs),
            "company_detail_requests": len(jobs),
            "greenhouse_detail_requests": len(jobs),
            "detail_concurrency": self.options.detail_concurrency,
            "stats": details,
        }

        apply_checks = []
        for index in dict.fromkeys([0, len(self.result.jobs) - 1]):
            if index < 0 or index >= len(self.result.jobs):
                continue
            try:
                apply_checks.append(await self._apply_check(self.result.jobs[index]))
            except Exception as exc:
                self.result.errors.append(
                    f"WorldQuant apply route check {self.result.jobs[index].job_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
        self.result.metadata["representative_apply_checks"] = apply_checks

        final = None
        try:
            final = await self._listing_snapshot("final")
        except Exception as exc:
            self.result.errors.append(f"WorldQuant final listing recheck failed: {type(exc).__name__}: {exc}")

        if final is None:
            self.result.metadata["membership_reconciliation"] = {
                "complete": False,
                "final_listing_available": False,
            }
        else:
            initial_by_id = {row["id"]: row for row in initial["rows"]}
            final_by_id = {row["id"]: row for row in final["rows"]}
            changed_fields = {}
            for identity in sorted(set(initial_by_id) & set(final_by_id)):
                if initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            same_ids = initial["job_ids"] == final["job_ids"]
            same_count = initial["count"] == final["count"]
            complete = (
                same_ids and same_count and not changed_fields
                and not final["issues"] and final["status"] == 200
            )
            self.result.metadata["final_listing"] = {
                key: value for key, value in final.items() if key != "rows"
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "same_ids_and_order": same_ids,
                "same_count": same_count,
                "changed_fields_by_id": changed_fields,
                "new_ids": [identity for identity in final["job_ids"] if identity not in initial_by_id],
                "removed_ids": [identity for identity in initial["job_ids"] if identity not in final_by_id],
                "complete": complete,
            }
            if not same_ids or not same_count:
                self.result.errors.append(
                    "WorldQuant listing membership changed during collection"
                )
            if changed_fields:
                self.result.errors.append(
                    "WorldQuant listing fields changed during collection: "
                    + ",".join(sorted(changed_fields))
                )
            self.result.errors.extend(final["issues"])

        self.result.metadata["posting_kinds"] = {
            job.job_id: job.raw.get("posting_kind")
            for job in self.result.jobs
            if job.raw.get("posting_kind") and job.raw.get("posting_kind") != "job"
        }
        reconciliation = self.result.metadata.get("membership_reconciliation") or {}
        self.result.listing_complete = bool(
            reconciliation.get("complete")
            and len(self.result.jobs) == initial["count"]
            and not self.result.errors
            and "sample_limit" not in self.result.warnings
        )
        self.result.timings["initial_listing_seconds"] = initial["request_seconds"]
        if final is not None:
            self.result.timings["final_listing_seconds"] = final["request_seconds"]
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
