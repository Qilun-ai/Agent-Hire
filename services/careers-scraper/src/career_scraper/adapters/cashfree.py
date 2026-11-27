"""Cashfree's first-party career page, its exact Kula embed, and that widget's API."""
from __future__ import annotations

from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.cashfree.com/careers/"
KULA_LISTING_URL = "https://careers.kula.ai/cashfree?jobs=true"
KULA_ORIGIN = "https://careers.kula.ai"
KULA_JOBS_API = KULA_ORIGIN + "/api/internal/ats_job_posts"
KULA_FILTER_OPTIONS_API = KULA_ORIGIN + "/api/internal/ats_job_posts/filter_options"
ACCOUNT_NAME = "cashfree"
ACCOUNT_ID = 1291
EMPLOYER_NAME = "Cashfree Payments India Private Limited"
ITEMS_PER_PAGE = 99
JOB_LIST_TYPE = "ats_job_post.index"
FILTER_OPTIONS_TYPE = "ats_job_post.filter_options"
JOB_LINK_PATH = re.compile(r"^/cashfree/(\d+)-([a-z0-9-]+)$", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.cashfree.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Cashfree official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    embeds = soup.select("iframe#kula_embed")
    if len(embeds) != 1:
        raise ValueError("Cashfree official careers page no longer exposes one Kula job-listing iframe")
    iframe = embeds[0]
    src = urljoin(response_url, iframe.get("src", ""))
    if (src != KULA_LISTING_URL or iframe.get("title") != "Cashfree Careers Job Listings"):
        raise ValueError("Cashfree company-published job-listing iframe URL or title changed")
    return {
        "url": response_url,
        "iframe_id": iframe.get("id"),
        "iframe_title": iframe.get("title"),
        "iframe_url": src,
        "company_hiring_contact": [
            anchor.get("href")
            for anchor in soup.select('a[href^="mailto:"]')
            if "careers" in anchor.get("href", "").lower()
        ],
    }


def parse_widget_listing(html: str, response_url: str = KULA_LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != "careers.kula.ai"
            or parsed.path != "/cashfree" or query != {"jobs": ["true"]}):
        raise ValueError("Cashfree embedded listing response came from an unexpected Kula route")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    app_name = soup.select_one('meta[name="application-name"]')
    employer = app_name.get("content") if app_name else None
    if "Cashfree" not in page_title or employer != EMPLOYER_NAME:
        raise ValueError("Cashfree embedded listing company identity changed")
    if not any(normalized_title(h.get_text(" ", strip=True)) == "Open Positions" for h in soup.select("h1,h2,h3")):
        raise ValueError("Cashfree embedded listing heading is missing")

    rows, seen, issues = [], set(), []
    for anchor in soup.select('a[href*="/cashfree/"]'):
        target = urljoin(response_url, anchor.get("href", ""))
        target_parts = urlsplit(target)
        match = JOB_LINK_PATH.fullmatch(target_parts.path)
        if match is None or target_parts.netloc != "careers.kula.ai" or parse_qs(target_parts.query) != {"jobs": ["true"]}:
            continue
        identity, slug = match.groups()
        if identity in seen:
            issues.append(f"duplicate_company_job_link:{identity}")
            continue
        card = anchor.find_parent("div", class_="chakra-card")
        paragraphs = card.select("p.chakra-text") if card else []
        department_node = card.select_one("span.chakra-stack > span") if card else None
        if not card or len(paragraphs) < 3 or department_node is None:
            issues.append(f"company_job_card_fields_missing:{identity}")
            continue
        work_text = paragraphs[2].get_text(" ", strip=True)
        employment_label, separator, workplace_label = work_text.partition("•")
        if not separator:
            issues.append(f"company_job_card_workplace_missing:{identity}")
        rows.append({
            "id": identity,
            "slug": slug,
            "url": target,
            "title": paragraphs[0].get_text(" ", strip=True),
            "department": department_node.get_text(" ", strip=True),
            "location": paragraphs[1].get_text(" ", strip=True),
            "employment_label": employment_label.strip(),
            "workplace_label": workplace_label.strip() if separator else None,
            "cta_label": anchor.get_text(" ", strip=True),
            "card_html": str(card),
        })
        seen.add(identity)
    if not rows:
        issues.append("cashfree_company_listing_has_no_exposed_job_rows")

    buttons = [b.get_text(" ", strip=True) for b in soup.select("button")]
    search = bool(soup.select_one('input[placeholder="Search"]'))
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "page_title": page_title,
        "employer": employer,
        "filters_visible": buttons,
        "keyword_search_available": search,
        "pagination_controls": [
            b.get_text(" ", strip=True) for b in soup.select("button,a")
            if re.search(r"\b(?:next|previous|load more)\b", b.get_text(" ", strip=True), re.I)
        ],
        "issues": issues,
    }


def parse_job_api_page(value: dict, requested_page: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Cashfree Kula jobs API response is not an object")
    errors = value.get("errors")
    if errors:
        raise ValueError(f"Cashfree Kula jobs API returned errors: {errors}")
    data = value.get("data")
    meta = value.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise ValueError("Cashfree Kula jobs API response is missing data or meta")
    count, page, items, pages = (meta.get(key) for key in ("count", "page", "items", "pages"))
    if (not all(isinstance(x, int) and x >= 0 for x in (count, page, items, pages))
            or page != requested_page or items != ITEMS_PER_PAGE or pages < 1
            or len(data) > ITEMS_PER_PAGE):
        raise ValueError("Cashfree Kula jobs API pagination schema changed")

    rows, seen, issues = [], set(), []
    for row in data:
        if not isinstance(row, dict):
            issues.append("cashfree_api_row_not_object")
            continue
        identity = str(row.get("id", ""))
        ats_job = row.get("ats_job")
        if not re.fullmatch(r"\d+", identity):
            issues.append("cashfree_api_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"cashfree_api_duplicate_id:{identity}")
            continue
        if row.get("account_id") != ACCOUNT_ID:
            issues.append(f"cashfree_api_row_wrong_account:{identity}")
            continue
        if row.get("listed") is not True or row.get("on_career_page") is not True or row.get("is_confidential") is not False:
            issues.append(f"cashfree_api_row_not_current_public_careers_posting:{identity}")
            continue
        if (not normalized_title(row.get("title")) or not isinstance(ats_job, dict)
                or not isinstance(ats_job.get("job_description"), str)):
            issues.append(f"cashfree_api_row_source_fields_missing:{identity}")
            continue
        if len(normalized_body_text(ats_job["job_description"])) < 100:
            issues.append(f"cashfree_api_description_empty_or_truncated:{identity}")
        if not isinstance(ats_job.get("offices"), list):
            issues.append(f"cashfree_api_offices_missing:{identity}")
        seen.add(identity)
        rows.append(dict(row))
    if len(data) > count or count < len(rows):
        issues.append("cashfree_api_count_mismatch")
    expected_page_rows = min(items, max(0, count - (page - 1) * items))
    if len(data) != expected_page_rows:
        issues.append("cashfree_api_page_row_count_mismatch")
    return {"rows": rows, "count": count, "page": page, "items": items, "pages": pages, "issues": issues}


def parse_filter_options(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("errors"):
        raise ValueError(f"Cashfree Kula filter-options response changed: {value}")
    data = value.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("departments"), list) or not isinstance(data.get("offices"), list):
        raise ValueError("Cashfree Kula filter-options response is missing departments or offices")
    if any(not isinstance(row, dict) or not row.get("name") for row in data["departments"] + data["offices"]):
        raise ValueError("Cashfree Kula filter-options response contains an unidentified option")
    return data


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-cashfree-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-cashfree-body]")
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
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def detail_page_reference(html: str, expected_row: dict, expected_url: str) -> dict:
    """Check an exact company-linked detail page against its inline list-body field."""
    parts = urlsplit(expected_url)
    match = JOB_LINK_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != "careers.kula.ai" or match is None
            or match[1] != str(expected_row.get("id")) or parse_qs(parts.query) != {"jobs": ["true"]}):
        raise ValueError("Cashfree representative detail URL is not an exact exposed Kula job link")
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("h1")
    panel = next((node for node in soup.select('[role="tabpanel"]') if node.get("id", "").endswith("tabpanel-0")), None)
    body = None
    if panel is not None:
        expected = normalized_body_text(expected_row.get("ats_job", {}).get("job_description", ""))
        body = next((node for node in panel.find_all("div", recursive=False)
                     if normalized_body_text(str(node)) == expected), None)
    api_title = normalized_title(expected_row.get("title"))
    actual_title = normalized_title(title.get_text(" ", strip=True)) if title else ""
    if not title or actual_title != api_title or body is None:
        raise ValueError("Cashfree exact detail title or full API-matching body is missing")
    buttons = [b.get_text(" ", strip=True) for b in soup.select("button")]
    tabs = [b.get_text(" ", strip=True) for b in soup.select('[role="tab"]')]
    return {
        "url": expected_url,
        "title": actual_title,
        "description_html": str(body),
        "description_text": normalized_body_text(str(body)),
        "description_normalized_chars": len(normalized_body_text(str(body))),
        "description_fnv1a64": fnv1a64(normalized_body_text(str(body))),
        "matches_api_description_text": True,
        "apply_controls": buttons,
        "application_tabs": tabs,
        "application_form_excluded_from_description": all("Application form" not in normalized_body_text(str(body)) for _ in [0]),
        "file_inputs_not_role_attachments": [x.get("accept") for x in soup.select('input[type="file"]')],
    }


def _api_params(page: int) -> dict:
    # This is the observed request used by the embedded widget's initial and
    # pagination effects; undefined scope/filter values are omitted by qs.
    return {"accountName": ACCOUNT_NAME, "page": page, "type": JOB_LIST_TYPE, "items": ITEMS_PER_PAGE}


class Cashfree(Adapter):
    """Read only the Cashfree careers iframe and its account-scoped Kula job feed."""

    VERIFICATION_REVISION = "cashfree.kula-company-embed-api.1"

    def __init__(self, http, company, source, options, *, initial_capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.initial_capture = initial_capture

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(status=response.status_code, seconds=seconds, response_sha256=sha256(response.content).hexdigest(), response_bytes=len(response.content))
        return entry

    async def _widget_listing(self):
        started = time.perf_counter()
        response = await self.http.get(KULA_LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != KULA_LISTING_URL:
            raise ValueError("Cashfree Kula iframe route redirected away from the company listing")
        parsed = parse_widget_listing(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds, response_sha256=sha256(response.content).hexdigest(), response_bytes=len(response.content))
        return parsed

    async def _filter_options(self):
        started = time.perf_counter()
        response = await self.http.request(
            "GET", KULA_FILTER_OPTIONS_API,
            params={"accountName": ACCOUNT_NAME, "type": FILTER_OPTIONS_TYPE},
            headers={"Origin": KULA_ORIGIN, "Referer": KULA_LISTING_URL},
        )
        seconds = max(0.0, time.perf_counter() - started)
        if urlsplit(str(response.url)).hostname != "careers.kula.ai" or urlsplit(str(response.url)).path != "/api/internal/ats_job_posts/filter_options":
            raise ValueError("Cashfree Kula filter options redirected away from its company-scoped route")
        parsed = parse_filter_options(response.json())
        return {"data": parsed, "url": str(response.url), "seconds": seconds,
                "status": response.status_code, "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content)}

    async def _api_snapshot(self, *, capture: dict | None = None):
        started = time.perf_counter()
        pages, rows, seen = [], [], set()
        expected_count = expected_pages = None
        issues = []
        page = 1
        if capture is not None:
            if not self.page_guard():
                return {"rows": [], "job_ids": [], "count": None, "total_pages": None,
                        "pages": [], "issues": ["cashfree_reused_initial_page_budget_exhausted"]}
            page_value = parse_job_api_page(capture["json"], 1)
            if page_value["pages"] != 1:
                issues.append("cashfree_reused_snapshot_does_not_include_all_api_pages")
            issues.extend(page_value["issues"])
            rows = page_value["rows"]
            seen.update(str(row["id"]) for row in rows)
            pages.append({
                "page": 1,
                "count": page_value["count"],
                "items": page_value["items"],
                "total_pages": page_value["pages"],
                "job_ids": [str(row["id"]) for row in rows],
                "request_url": capture["request_url"],
                "final_url": capture["final_url"],
                "request_seconds": capture["request_seconds"],
                "response_bytes": capture["response_bytes"],
                "response_sha256": capture["response_sha256"],
                "reused_source_capture": True,
                "arrival_timestamp_recorded": False,
            })
            return {
                "rows": rows,
                "job_ids": [str(row["id"]) for row in rows],
                "count": page_value["count"],
                "total_pages": page_value["pages"],
                "pages": pages,
                "issues": list(dict.fromkeys(issues)),
                "request_seconds": max(0.0, time.perf_counter() - started),
                "source_request_seconds": capture["request_seconds"],
                "reused_source_capture": True,
            }
        while True:
            if not self.page_guard():
                issues.append("cashfree_api_page_budget_exhausted")
                break
            params = _api_params(page)
            request_started = time.perf_counter()
            response = await self.http.request("GET", KULA_JOBS_API, params=params,
                                               headers={"Origin": KULA_ORIGIN, "Referer": KULA_LISTING_URL})
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed_url = urlsplit(str(response.url))
            if parsed_url.scheme != "https" or parsed_url.netloc != "careers.kula.ai" or parsed_url.path != "/api/internal/ats_job_posts":
                raise ValueError("Cashfree Kula jobs API redirected outside the company-scoped widget route")
            page_value = parse_job_api_page(response.json(), page)
            if expected_count is None:
                expected_count, expected_pages = page_value["count"], page_value["pages"]
            elif (page_value["count"], page_value["pages"], page_value["items"]) != (expected_count, expected_pages, ITEMS_PER_PAGE):
                issues.append("cashfree_api_pagination_metadata_changed")
            issues.extend(page_value["issues"])
            for row in page_value["rows"]:
                identity = str(row["id"])
                if identity in seen:
                    issues.append(f"cashfree_api_duplicate_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({
                "page": page,
                "count": page_value["count"],
                "items": page_value["items"],
                "total_pages": page_value["pages"],
                "job_ids": [str(row["id"]) for row in page_value["rows"]],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if expected_pages is None or expected_pages < 1:
                issues.append("cashfree_api_page_count_unknown")
                break
            # The inspected widget uses this same `page` parameter and reports
            # `meta.pages`. A changed multi-page live UI must be reviewed before
            # the worker requests any page beyond the rendered one.
            if expected_pages != 1:
                issues.append("cashfree_native_pagination_not_observed_for_current_company_page")
                break
            if page >= expected_pages:
                break
            page += 1
        if expected_count is not None and len(rows) != expected_count:
            issues.append("cashfree_api_total_does_not_match_retained_rows")
        return {
            "rows": rows,
            "job_ids": [str(row["id"]) for row in rows],
            "count": expected_count,
            "total_pages": expected_pages,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _make_job(self, row, card):
        identity = str(row["id"])
        ats_job = row.get("ats_job") or {}
        description_html = ats_job.get("job_description") or ""
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibility_sections = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].lower()
                                           for token in ("responsibilit", "what you will do", "what you'll do"))
        ]
        qualification_sections = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].lower()
                                           for token in ("qualif", "requirement", "what you will need",
                                                         "what you'll need", "who you are", "required skills"))
        ]
        offices = ats_job.get("offices") or []
        locations = [office.get("location") for office in offices if isinstance(office, dict) and office.get("location")]
        department = ats_job.get("ats_department")
        description_soup = BeautifulSoup(description_html, "lxml")
        links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
                 for a in description_soup.select("a[href]")]
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        job = self.job(
            {"listing": dict(row), "company_listing_card": card},
            title=normalized_title(card.get("title") or row.get("title")),
            url=card["url"],
            job_id=identity,
            requisition_id=None,
            apply_url=card["url"],
            description=description_html,
            method="cashfree.kula.ats_job_posts.ats_job.job_description",
            description_url=KULA_JOBS_API,
            locations=locations,
            department=department.get("name") if isinstance(department, dict) else None,
            employment_type=ats_job.get("work_type_name"),
            workplace_type=card.get("workplace_label"),
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=EMPLOYER_NAME,
            language="en",
            responsibilities=responsibility_sections or None,
            qualifications=qualification_sections or None,
        )
        job.description_text = description_text
        job.company_scope_verified = self.company.scope == "company"
        normalized = normalized_body_text(description_html)
        job.raw["detail"] = {
            "source_field": "ats_job.job_description",
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": sections,
            "description_links": links,
            "role_attachments": attachments,
            "dates": {"launch_at": row.get("launch_at"), "end_at": row.get("end_at"),
                      "posting_date_mapping": None, "closing_date_mapping": None},
            "application_route": {
                "listing_cta": card.get("cta_label"),
                "exact_job_page_url": card["url"],
                "detail_page_apply_control": "Apply for this position",
                "detail_page_form_tab": "Application form",
                "separate_form_action_url": None,
            },
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": KULA_LISTING_URL,
            "listing_authority": "The exact Kula job-listing iframe published by Cashfree's own careers page.",
            "listing_membership_request": {
                "method": "GET",
                "path": "/api/internal/ats_job_posts",
                "query_shape": {"accountName": ACCOUNT_NAME, "page": "UI page state", "type": JOB_LIST_TYPE, "items": ITEMS_PER_PAGE},
                "filters_applied": False,
                "scope": "Cashfree accountName only; no other Kula company or board is queried.",
                "bundle_evidence": "Kula's exact account page bundle exports the listing request and uses response meta.pages for UI pagination.",
            },
            "detail_method": "Full ats_job.job_description is returned inline by the same account-scoped UI listing request; representative exact detail URLs are checked independently.",
            "request_scope": "Only accountName=cashfree and exact page links exposed by its own embedded listing; no Kula-wide discovery.",
        })
        try:
            if self.initial_capture is None:
                initial_entry = await self._official_entry()
                initial_widget = await self._widget_listing()
                filter_options = await self._filter_options()
                initial = await self._api_snapshot()
            else:
                capture_started = time.perf_counter()
                capture = self.initial_capture
                initial_entry = parse_official_entry(capture["official_html"], capture["official_metadata"]["final_url"])
                initial_entry.update(capture["official_metadata"])
                initial_widget = parse_widget_listing(capture["widget_html"], capture["widget_metadata"]["final_url"])
                initial_widget.update(capture["widget_metadata"])
                filter_options = {
                    "data": parse_filter_options(capture["filter_json"]),
                    **capture["filter_metadata"],
                }
                initial = await self._api_snapshot(capture=capture["api_metadata"] | {"json": capture["api_json"]})
                self.result.timings["reused_initial_snapshot_parse_seconds"] = max(
                    0.0, time.perf_counter() - capture_started
                )
        except Exception as exc:
            self.result.errors.append(f"Cashfree source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_widget_listing"] = {
            k: v for k, v in initial_widget.items() if k != "rows"
        }
        self.result.metadata["initial_widget_job_ids"] = initial_widget["job_ids"]
        self.result.metadata["filter_options"] = filter_options
        self.result.metadata["initial_api_listing"] = {k: v for k, v in initial.items() if k != "rows"}
        if initial_widget["issues"]:
            self.result.errors.extend(initial_widget["issues"])
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
        if initial["count"] != len(initial_widget["rows"]) or initial["job_ids"] != initial_widget["job_ids"]:
            self.result.errors.append("cashfree_api_and_official_iframe_membership_mismatch")

        card_by_id = {card["id"]: card for card in initial_widget["rows"]}
        rows_by_id = {str(row["id"]): row for row in initial["rows"]}
        jobs, detail_parse_seconds = [], 0.0
        for identity in initial_widget["job_ids"]:
            row = rows_by_id.get(identity)
            card = card_by_id.get(identity)
            if row is None or card is None:
                self.result.errors.append(f"cashfree_linked_job_missing_api_row:{identity}")
                continue
            ats_job = row.get("ats_job") or {}
            api_title = normalized_title(row.get("title"))
            if api_title != normalized_title(card.get("title")):
                self.result.errors.append(f"cashfree_api_card_title_mismatch:{identity}")
            department = ats_job.get("ats_department")
            api_department = department.get("name") if isinstance(department, dict) else None
            if api_department != card.get("department"):
                self.result.errors.append(f"cashfree_api_card_department_mismatch:{identity}")
            offices = ats_job.get("offices") or []
            api_locations = [o.get("location") for o in offices if isinstance(o, dict) and o.get("location")]
            if api_locations and card.get("location") not in api_locations:
                self.result.errors.append(f"cashfree_api_card_location_mismatch:{identity}")
            if ats_job.get("work_type_name") and card.get("employment_label") != ats_job.get("work_type_name"):
                self.result.errors.append(f"cashfree_api_card_employment_mismatch:{identity}")
            workplace_label = {"office": "On-Site", "remote": "Remote", "hybrid": "Hybrid"}.get(ats_job.get("workplace"))
            if workplace_label and card.get("workplace_label") != workplace_label:
                self.result.errors.append(f"cashfree_api_card_workplace_mismatch:{identity}")
            detail_started = time.perf_counter()
            job = self._make_job(row, card)
            detail_parse_seconds += max(0.0, time.perf_counter() - detail_started)
            if job.raw["detail"]["role_attachments"]:
                self.result.errors.append(f"cashfree_role_attachments_require_exact_review:{identity}")
            jobs.append(job)
        if len(jobs) != initial["count"]:
            self.result.errors.append("cashfree_retained_rows_do_not_match_company_listing_count")

        final = None
        try:
            final_entry = await self._official_entry()
            final_widget = await self._widget_listing()
            final_filter_options = await self._filter_options()
            final = await self._api_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Cashfree final source recheck failed: {type(exc).__name__}: {exc}")
            final_widget = None
            final_entry = None
            final_filter_options = None
        if final is None or final_widget is None or final_entry is None:
            final_matches = False
        else:
            final_ids = final["job_ids"]
            changed_fields = {}
            final_by_id = {str(row["id"]): row for row in final["rows"]}
            initial_by_id = {str(row["id"]): row for row in initial["rows"]}
            for identity in initial["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            counts_match = (initial["count"], initial["total_pages"]) == (final["count"], final["total_pages"])
            same_ids = initial["job_ids"] == final_ids
            same_widget_ids = initial_widget["job_ids"] == final_widget["job_ids"]
            entry_embed_same = initial_entry["iframe_url"] == final_entry["iframe_url"] == KULA_LISTING_URL
            same_filter_options = filter_options.get("data") == final_filter_options.get("data")
            final_matches = (same_ids and same_widget_ids and counts_match and not changed_fields
                             and not final["issues"] and not final_widget["issues"] and entry_embed_same
                             and same_filter_options)
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_widget_listing"] = {k: v for k, v in final_widget.items() if k != "rows"}
            self.result.metadata["final_widget_job_ids"] = final_widget["job_ids"]
            self.result.metadata["final_api_listing"] = {k: v for k, v in final.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final_ids,
                "initial_widget_ids": initial_widget["job_ids"],
                "final_widget_ids": final_widget["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_pages": initial["total_pages"],
                "final_pages": final["total_pages"],
                "api_and_widget_membership_match": initial["job_ids"] == initial_widget["job_ids"],
                "same_ids_and_order": same_ids,
                "same_widget_ids_and_order": same_widget_ids,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final_ids if i not in initial["job_ids"]],
                "removed_job_ids": [i for i in initial["job_ids"] if i not in final_ids],
                "official_embed_still_exact": entry_embed_same,
                "filter_options_unchanged": same_filter_options,
                "complete": final_matches,
            }
            if not entry_embed_same:
                self.result.errors.append("cashfree_official_embed_changed_during_collection")
            if not same_ids or not same_widget_ids or not counts_match:
                self.result.errors.append("cashfree_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("cashfree_source_fields_changed_during_collection")
            if not same_filter_options:
                self.result.errors.append("cashfree_filter_options_changed_during_collection")
            self.result.errors.extend(final["issues"])
            self.result.errors.extend(final_widget["issues"])

        self.result.jobs = jobs
        self.result.timings["inline_detail_parse_seconds_sum"] = detail_parse_seconds
        self.result.timings["detail_phase_seconds"] = detail_parse_seconds
        self.result.metadata["detail_hydration_mode"] = "Full ats_job.job_description is inline in the exact company-scoped listing API response; no detail fetch per row."
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["api_meta_count"] = initial["count"]
        self.result.metadata["api_page_count"] = initial["total_pages"]
        self.result.metadata["widget_visible_count"] = initial_widget["count"]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(final_matches and len(jobs) == initial["count"])
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
