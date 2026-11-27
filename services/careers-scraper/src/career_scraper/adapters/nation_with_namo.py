"""Nation with Namo's own careers subdomain and its inline Open Positions accordion.

The ledger URL (https://www.nationwithnamo.com/careers) currently returns 404 and
the www root redirects to the GILP program site, which publishes no job listing.
The company-owned careers host is live and publishes the exact Open Positions
membership collected here.  That listing has no per-job detail pages: every full
role body is inside the collapsed card on the same page, and every Start Your
Application control loads the shared apply.html with a localStorage position
token.  No request-time or template date is treated as a posting date.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.nationwithnamo.com/careers"
CAREERS_ORIGIN = "https://careers.nationwithnamo.com"
LISTING_URL = CAREERS_ORIGIN + "/"
APPLY_URL = CAREERS_ORIGIN + "/apply.html"
GILP_URL = "https://gilp.nationwithnamo.com"
COMPANY_NAME = "Nation with Namo"
EMPLOYER = "Nation with NaMo"
EXPECTED_CATEGORIES = (
    "Consulting",
    "Policy & Governance",
    "Product, Technology & Analytics",
    "Creative Communications",
)
POSITION_CALL = re.compile(r'set_position\("([^"]*)"\)')


def normalize_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _listing_url_ok(response_url: str) -> bool:
    parsed = urlsplit(response_url)
    return (parsed.scheme == "https" and parsed.netloc == "careers.nationwithnamo.com"
            and parsed.path in ("", "/", "/index.html") and not parsed.query and not parsed.fragment)


def _card_title(card: Tag) -> str:
    header = card.select_one(".card-header")
    if header is None:
        return ""
    # The source's <h4> is closed before the title <p> siblings.  Reading the
    # first <p> reproduces the rendered title; h4 text is empty.
    paragraph = header.find("p")
    return normalize_ws(paragraph.get_text(" ", strip=True) if paragraph is not None else "")


def _sections(body: Tag) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None
    for child in body.children:
        if isinstance(child, Tag) and child.name in ("h4", "h5", "h6"):
            current = {"heading": normalize_ws(child.get_text(" ", strip=True)), "html": ""}
            sections.append(current)
        elif current is not None:
            current["html"] += str(child)
    for section in sections:
        section["html"] = section["html"].strip()
        section["text"] = text_html(section["html"])
    return sections


def _section_text(sections: list[dict], pattern: str) -> list[dict] | None:
    found = [{"heading": section["heading"], "text": section["text"]}
             for section in sections if re.search(pattern, section["heading"], re.I)]
    return found or None


def parse_listing(html: str, response_url: str = LISTING_URL, *, headers: dict | None = None) -> dict:
    """Parse the exact company-owned Open Positions page.

    Raises ValueError when the company identity, listing container, or any
    card's title/body is missing.  Source quirks (duplicate collapse ids and
    apply-token/title mismatches) are retained as issues, not silently fixed.
    """
    if not _listing_url_ok(response_url):
        raise ValueError("Nation with Namo listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_ws(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Work with us at Nation with NaMo":
        raise ValueError(f"Nation with Namo listing identity changed: {title!r}")
    heading = soup.find(id="Open_Position")
    if heading is None or "open positions" not in heading.get_text(" ", strip=True).lower():
        raise ValueError("Nation with Namo Open Positions heading is missing")
    container = heading.find_parent("div", class_="container")
    row = container.find("div", class_="row", recursive=False) if container else None
    if row is None:
        raise ValueError("Nation with Namo Open Positions row is missing")

    groups: list[dict] = []
    rows: list[dict] = []
    issues: list[str] = []
    seen_anchors: dict[str, int] = {}
    for group in row.find_all("div", recursive=False):
        classes = " ".join(group.get("class") or [])
        if "col-md-10" not in classes:
            continue
        category_node = group.find("h4")
        if category_node is None:
            issues.append("category_heading_missing")
            continue
        category = normalize_ws(category_node.get_text(" ", strip=True))
        cards = group.select("div.card.my-3")
        if not cards:
            issues.append(f"category_has_no_cards:{category}")
        groups.append({"category": category, "cards": len(cards)})
        for card in cards:
            card_index = len(rows)
            header = card.select_one(".card-header")
            h4 = header.find("h4") if header is not None else None
            target = (h4.get("data-target") if h4 is not None else None) or ""
            anchor = target[1:] if target.startswith("#") else target
            if header is None or h4 is None or not anchor:
                raise ValueError(f"Open Positions card {card_index} has no collapse target")
            card_title = _card_title(card)
            if not card_title:
                raise ValueError(f"Open Positions card {card_index} has no visible title")
            body = card.select_one(".card-body")
            if body is None:
                raise ValueError(f"Open Positions card {card_index} has no description body")
            card_html = str(card)
            apply_button = body.select_one(".position_button")
            apply_token = None
            if apply_button is not None:
                match = POSITION_CALL.search(apply_button.get("onclick", "") or "")
                apply_token = match.group(1) if match else None
                wrapper = apply_button.find_parent("p")
                if wrapper is not None and not normalize_ws(wrapper.get_text(" ", strip=True)):
                    wrapper.decompose()
                else:
                    apply_button.decompose()
            if apply_token is None:
                issues.append(f"apply_token_missing:{card_title}")
            description_html = body.decode_contents().strip()
            description_text = text_html(description_html)
            if len(normalize_ws(description_text)) < 100:
                raise ValueError(f"Open Positions card {card_index} has an empty/short description")
            sections = _sections(body)
            if not sections:
                issues.append(f"description_sections_missing:{card_title}")

            token_mismatch = normalize_ws(apply_token) != normalize_ws(card_title)
            if token_mismatch:
                issues.append(f"apply_token_differs_from_visible_title:{card_title}")
            if anchor in seen_anchors:
                issues.append(f"duplicate_collapse_anchor:{anchor}:{seen_anchors[anchor]}:{card_index}")
            else:
                seen_anchors[anchor] = card_index

            rows.append({
                "title": card_title,
                "category": category,
                "anchor": anchor,
                "card_index": card_index,
                "apply_token": apply_token,
                "apply_token_differs_from_visible_title": token_mismatch,
                "description_html": description_html,
                "description_text": description_text,
                "description_sha256": sha256(normalize_ws(description_text).encode()).hexdigest(),
                "sections": sections,
                "card_html": card_html,
            })

    if not rows:
        raise ValueError("Nation with Namo Open Positions exposes no job cards")
    categories = [group["category"] for group in groups]
    unexpected = [category for category in categories if category not in EXPECTED_CATEGORIES]
    missing = [category for category in EXPECTED_CATEGORIES if category not in categories]
    if unexpected or missing:
        issues.append(f"category_membership_changed:unexpected={unexpected}:missing={missing}")

    pagination = []
    for control in soup.select("button,a"):
        label = normalize_ws(control.get_text(" ", strip=True))
        if re.search(r"\b(?:next|previous|load more|page \d+)\b", label, re.I):
            pagination.append(label)
    filters = []
    for control in soup.select("select,input[type=search],input[placeholder]"):
        filters.append({"tag": control.name, "type": control.get("type"), "placeholder": control.get("placeholder"),
                        "name": control.get("name")})
    if pagination:
        issues.append(f"pagination_controls_present:{pagination}")
    if filters:
        issues.append(f"filters_present:{filters}")

    headers = headers or {}
    page_text = normalize_ws(soup.get_text(" ", strip=True))
    contract_rows = [row["title"] for row in rows
                     if re.search(r"contract", row["description_text"], re.I) and "2021" in row["description_text"]]
    return {
        "rows": rows,
        "count": len(rows),
        "categories": categories,
        "groups": groups,
        "page_title": title,
        "listing_url": response_url,
        "pagination_controls": pagination,
        "filters": filters,
        "issues": issues,
        "headers": {key: headers.get(key) for key in ("last-modified", "etag", "content-length", "date")},
        "source_page_sha256": sha256(html.encode()).hexdigest(),
        "expected_categories": list(EXPECTED_CATEGORIES),
        "legacy_markers": {
            "application_modal_next_round_january_2020": {
                "present_in_html": "January 2020" in html,
                "visible_in_rendered_text": "January 2020" in page_text,
                "note": "Present only inside a commented-out modal in the current source; not rendered.",
            },
            "applications_closed_modal_present": {
                "present_in_html": "Applications for the Graduate Impact Leadership Program are now closed" in html,
                "visible_in_rendered_text": "Applications for the Graduate Impact Leadership Program are now closed" in page_text,
            },
            "role_contract_till_may_2021": contract_rows,
        },
    }


def parse_apply_page(html: str, response_url: str = APPLY_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.nationwithnamo.com"
            or parsed.path != "/apply.html" or parsed.query):
        raise ValueError("Nation with Namo apply page came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_ws(soup.title.get_text(" ", strip=True) if soup.title else "")
    form = soup.find("form", id="myform")
    resume = soup.find("input", id="resume")
    if form is None or resume is None:
        raise ValueError("Nation with Namo shared apply form or resume input is missing")
    inline = "\n".join(script.get_text() for script in soup.find_all("script") if not script.get("src"))
    if "localStorage.getItem('position')" not in inline or "api/v1/entry" not in inline:
        raise ValueError("Nation with Namo apply page no longer routes the position token to its application API")
    fields = []
    for control in form.select("input[name],select[name],textarea[name]"):
        fields.append({"name": control.get("name"), "type": control.get("type"), "id": control.get("id")})
    return {
        "title": title,
        "form_id": form.get("id"),
        "fields": fields,
        "resume_input_present": resume is not None,
        "position_field_present": any(field["name"] == "position" for field in fields),
        "position_from_localstorage": True,
        "application_api": "https://gilp.nationwithnamo.com/api/v1/entry",
        "note": "One shared apply.html for every role; no per-job URL or visible position field is published.",
    }


class NationWithNamo(Adapter):
    VERIFICATION_REVISION = "nation-with-namo.official-careers-subdomain.inline-open-positions.1"

    async def _fetch_listing(self, *, phase: str) -> dict:
        if not self.page_guard():
            raise ValueError("Nation with Namo listing request budget exhausted")
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        elapsed = max(0.0, time.perf_counter() - started)
        if not _listing_url_ok(str(response.url)):
            raise ValueError(f"Nation with Namo listing redirected away to {response.url}")
        parse_started = time.perf_counter()
        parsed = parse_listing(response.text, str(response.url), headers=dict(response.headers))
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        parsed["http"] = {
            "status": response.status_code,
            "final_url": str(response.url),
            "bytes": len(response.content),
            "sha256": sha256(response.content).hexdigest(),
            "request_seconds": round(elapsed, 6),
            "parse_seconds": round(parse_seconds, 6),
            "last_modified": response.headers.get("last-modified"),
            "etag": response.headers.get("etag"),
            "phase": phase,
        }
        self.result.timings[f"listing_{phase}_seconds"] = elapsed
        self.result.timings["inline_description_parse_seconds"] = (
            self.result.timings.get("inline_description_parse_seconds", 0.0) + parse_seconds)
        return parsed

    def _add_rows(self, parsed: dict):
        for row in parsed["rows"]:
            raw = {
                "source": "careers.nationwithnamo.com",
                "category": row["category"],
                "card_index": row["card_index"],
                "anchor": row["anchor"],
                "visible_title": row["title"],
                "apply_position_token": row["apply_token"],
                "apply_token_differs_from_visible_title": row["apply_token_differs_from_visible_title"],
                "card_html": row["card_html"],
                "sections": row["sections"],
                "listing_page_sha256": parsed["source_page_sha256"],
                "listing_url": parsed["listing_url"],
                "apply_url": APPLY_URL,
                "employment_note": next((section["text"] for section in row["sections"]
                                         if re.search(r"contract", section["text"], re.I)), None),
                "listing": {
                    "title": row["title"], "category": row["category"], "anchor": row["anchor"],
                    "card_index": row["card_index"], "apply_token": row["apply_token"],
                    "apply_token_differs_from_visible_title": row["apply_token_differs_from_visible_title"],
                    "sections": row["sections"],
                },
            }
            job = self.job(
                raw,
                title=row["title"],
                url=LISTING_URL + "#" + row["anchor"],
                job_id=None,
                apply_url=APPLY_URL,
                description=row["description_html"],
                method="nation_with_namo.open_positions_accordion_full_html",
                description_url=LISTING_URL + "#" + row["anchor"],
                department=row["category"],
                employer=EMPLOYER,
                locations=[],
                qualifications=_section_text(row["sections"], r"requirement|qualification"),
                responsibilities=_section_text(row["sections"], r"about the role|responsibilit|duties"),
            )
            job.record_identity = sha256(
                f"{self.company.name}|{row['category']}|{row['title']}|{row['apply_token']}|{row['anchor']}".encode()
            ).hexdigest()
            if row["apply_token_differs_from_visible_title"]:
                job.warnings.append("apply_token_differs_from_visible_title")
            if row["anchor"] == "comms-two":
                job.warnings.append("duplicate_source_collapse_anchor")
            self.add(job)

    async def run(self) -> dict:
        started = time.perf_counter()
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry_url=OFFICIAL_ENTRY,
            official_entry_status=None,
            official_entry_error=None,
            listing_authority="company-owned careers subdomain (not linked from the current www->GILP redirect)",
            listing_url=LISTING_URL,
            apply_url=APPLY_URL,
            gilp_program_url=GILP_URL,
            description_method="inline collapsed card body on the Open Positions page",
            dates_published=False,
            ids_published=False,
            locations_published=False,
            salary_published=False,
        )

        entry_started = time.perf_counter()
        try:
            entry = await self.http.get(OFFICIAL_ENTRY)
            self.result.metadata["official_entry_status"] = entry.status_code
            self.result.metadata["official_entry_final_url"] = str(entry.url)
            if not _listing_url_ok(str(entry.url)):
                self.result.warnings.append("official_entry_redirected_away_from_listing")
        except FetchError as exc:
            message = str(exc)
            self.result.metadata["official_entry_error"] = message
            match = re.search(r"HTTP\s+(\d{3})", message)
            self.result.metadata["official_entry_status"] = int(match.group(1)) if match else None
            self.result.errors.append(f"official_entry_unreachable: {message}")
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)

        initial = await self._fetch_listing(phase="initial")
        self._add_rows(initial)
        self.result.expected_total = initial["count"]
        blocking = [issue for issue in initial["issues"] if issue.startswith("pagination_controls_present")]
        self.result.listing_complete = initial["count"] > 0 and not blocking
        if initial["issues"]:
            self.result.warnings.extend(initial["issues"])

        apply_started = time.perf_counter()
        try:
            apply_response = await self.http.get(APPLY_URL)
            apply_page = parse_apply_page(apply_response.text, str(apply_response.url))
            apply_page.update({"status": apply_response.status_code, "final_url": str(apply_response.url),
                               "bytes": len(apply_response.content),
                               "sha256": sha256(apply_response.content).hexdigest()})
            self.result.metadata["apply_page"] = apply_page
        except Exception as exc:
            self.result.errors.append(f"apply_route_unreachable: {type(exc).__name__}: {exc}")
        self.result.timings["apply_route_seconds"] = max(0.0, time.perf_counter() - apply_started)

        final = await self._fetch_listing(phase="final")
        initial_signature = [(row["category"], row["title"], row["apply_token"], row["anchor"],
                              row["description_sha256"]) for row in initial["rows"]]
        final_signature = [(row["category"], row["title"], row["apply_token"], row["anchor"],
                            row["description_sha256"]) for row in final["rows"]]
        same_order = initial_signature == final_signature
        reconciliation = {
            "initial_count": initial["count"],
            "final_count": final["count"],
            "initial_ids": [row["title"] for row in initial["rows"]],
            "final_ids": [row["title"] for row in final["rows"]],
            "initial_final_order_and_body_hashes_match": same_order,
            "initial_http": initial["http"],
            "final_http": final["http"],
            "pagination_controls": initial["pagination_controls"],
            "filters": initial["filters"],
            "categories": initial["categories"],
            "complete": bool(initial["count"] == final["count"] == len(self.result.jobs) and same_order
                             and initial["count"] > 0),
        }
        if not reconciliation["complete"]:
            self.result.listing_complete = False
            self.result.errors.append("listing_changed_during_collection")
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata.update({
            "listing_page_title": initial["page_title"],
            "listing_categories": initial["categories"],
            "listing_groups": initial["groups"],
            "visible_listing_rows": initial["count"],
            "pagination": "none; one page, no next/previous/load-more controls",
            "filters": "none",
            "source_issue_list": initial["issues"],
            "source_page_sha256": initial["source_page_sha256"],
            "details": {
                "total": len(self.result.jobs),
                "checked": len(self.result.jobs),
                "fetched_jobs": 0,
                "verified_cache_hits": 0,
                "unavailable_cache_hits": 0,
                "failed": 0,
                "duration_seconds": 0.0,
                "inline_company_listing_bodies": True,
                "per_job_detail_requests": 0,
            },
            "source_staleness_indicators": {
                **initial["legacy_markers"],
                "last_modified_header": initial["http"].get("last_modified"),
                "do_not_use_as_posting_dates": True,
                "note": "The page explicitly contains 2020/2021-era text; these markers are retained but never copied into date_posted/valid_through.",
            },
        })
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.timings["detail_phase_seconds"] = 0.0
        return self.result

