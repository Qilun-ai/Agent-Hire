"""Dozee's official careers page and its linked Lever board journey.

Observed live flow (2026-11-07, see ``companies/dozee/research/journey.md``):

- Entry ``https://www.dozeehealth.ai/careers`` publishes one "job openings"
  anchor to ``https://jobs.lever.co/dozee`` (target=_Blank). That anchor is the
  only job-board link on the official page; no other ATS/board link was found.
- ``GET https://jobs.lever.co/dozee`` SSR-renders every open posting card in a
  single page (no pagination). Each ``.posting`` carries
  ``data-qa-posting-id=<UUID>``, an ``h5[data-qa=posting-name]`` title, and
  workplace/commitment/location category spans, grouped under
  ``.postings-group`` large-category (department) and category-title (team)
  headers. Filter popups are query-string links (workplaceType/location/
  department/team/commitment); the unfiltered page is the full membership.
- Each card's title link is ``https://jobs.lever.co/dozee/<UUID>``. The exact
  detail page SSR-renders a ``JobPosting`` JSON-LD document with title,
  hiringOrganization name "Dozee", jobLocation addressLocality,
  employmentType, datePosted (YYYY-MM-DD) and the full ``description`` HTML
  (Company Overview through closing sections). The rendered Apply buttons
  point to ``https://jobs.lever.co/dozee/<UUID>/apply``. The generic
  ``data-qa="ai-disclaimer"`` hiring-AI notice is page chrome, not role
  description, and is excluded from the JSON-LD description.

Membership authority is the unfiltered Lever board published from Dozee's
official careers page. The collector requests only the board URL linked from
the official entry and the exact detail URLs listed on that board. It never
guesses a board slug, enumerates a Lever API, opens an application form, or
collects another tenant's board.

The isolated adapter is not registered in the shared dispatch; parent owns
that integration (see ``companies/dozee/README.md``).
"""
import hashlib
import html
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
BOARD_HOST = "jobs.lever.co"
BOARD_SLUG = "dozee"
BOARD_URL = "https://jobs.lever.co/dozee"
ENTRY_HOSTS = {"www.dozeehealth.ai", "dozeehealth.ai"}
AI_DISCLAIMER_MARKER = "We may use artificial intelligence (AI) tools"


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(board_url, identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"Dozee official-listing job ID is not a UUID: {identity!r}")
    parsed = urlsplit(str(board_url))
    if parsed.scheme != "https" or parsed.hostname != BOARD_HOST or parsed.path.strip("/") != BOARD_SLUG:
        raise ValueError(f"Dozee board URL changed from its observed Lever board: {board_url!r}")
    return f"{BOARD_URL}/{identity}"


def apply_url_for(board_url, identity):
    return native_job_url(board_url, identity) + "/apply"


def _clean(value):
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _salary_from_description(description_text):
    for line in (description_text or "").splitlines():
        value = line.strip()
        if not value:
            continue
        if not re.search(r"salary|pay range|OTE|compensation|per annum|per hour", value, re.I):
            continue
        if not re.search(r"[₹$€£]|/\s*(month|year|hour|annum)|lakh|crore", value, re.I):
            continue
        return {"source_text": value, "source_field": "detail_description"}
    return None


def _location_differs(board_location, detail_locality):
    if not board_location or not detail_locality:
        return False
    board_parts = {part.strip().lower() for part in re.split(r"/|;", str(board_location)) if part.strip()}
    detail_parts = [part.strip().lower() for part in re.split(r"/|;", str(detail_locality)) if part.strip()]
    return any(part not in board_parts for part in detail_parts)


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class Dozee(Adapter):
    """Follow only Dozee's official entry link to its published Lever board."""

    VERIFICATION_REVISION = "dozee.official-careers-lever-board-and-detail-jsonld.exact-ids.1"

    @staticmethod
    def _assert_entry_url(url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in ENTRY_HOSTS:
            raise ValueError(f"Dozee company page left the official HTTPS site: {url}")
        if parsed.path.rstrip("/") != "/careers":
            raise ValueError(f"Dozee company page has an unexpected path: {url}")

    @staticmethod
    def lever_board_url(entry_html, entry_url):
        soup = BeautifulSoup(entry_html or "", "lxml")
        candidates = sorted({
            anchor.get("href", "").strip()
            for anchor in soup.select("a[href]")
            if anchor.get("href", "").strip().rstrip("/") == BOARD_URL
        })
        if len(candidates) != 1:
            raise ValueError(
                f"Dozee official entry does not publish exactly one Lever board link (found {len(candidates)})"
            )
        parsed = urlsplit(candidates[0])
        if parsed.scheme != "https" or parsed.hostname != BOARD_HOST:
            raise ValueError(f"Dozee official entry links an unexpected job board: {candidates[0]}")
        actual = urljoin(entry_url, candidates[0])
        # Normalize the single observed board reference for request shapes.
        return BOARD_URL if actual.rstrip("/") == BOARD_URL else actual

    @staticmethod
    def _assert_beacon(board_html):
        # The SSR board must carry its filter bar and posting list; a bare or
        # error page must not be treated as an empty membership.
        if "postings-wrapper" not in (board_html or ""):
            raise ValueError("Dozee Lever board response has no postings list container")

    @staticmethod
    def parse_board(board_html, snapshot_name):
        Dozee._assert_beacon(board_html)
        soup = BeautifulSoup(board_html or "", "lxml")
        rows = []
        seen = set()
        for group in soup.select(".postings-group"):
            department = None
            dept_node = group.select_one(".large-category-header")
            if dept_node is not None:
                department = _clean(dept_node.get_text(" ", strip=True))
            team = None
            team_node = group.select_one(".posting-category-title")
            if team_node is not None:
                team = _clean(team_node.get_text(" ", strip=True))
            for posting in group.select(".posting"):
                identity = (posting.get("data-qa-posting-id") or "").strip()
                if not UUID.fullmatch(identity):
                    raise ValueError(
                        f"Dozee {snapshot_name} board contains a missing or invalid exact posting ID"
                    )
                if identity in seen:
                    raise ValueError(f"Dozee {snapshot_name} board repeats a company-published ID: {identity}")
                seen.add(identity)
                title_node = posting.select_one('[data-qa="posting-name"]')
                title = _clean(title_node.get_text(" ", strip=True)) if title_node is not None else None
                if not title:
                    raise ValueError(f"Dozee {snapshot_name} board posting {identity} has no title")
                workplace = None
                ws_node = posting.select_one(".posting-categories .workplaceTypes, .posting-categories .workplace-type")
                if ws_node is not None:
                    workplace = _clean(ws_node.get_text(" ", strip=True).rstrip("-").strip())
                    # Board renders "Remote —"; keep the bare label.
                    workplace = _clean(re.sub(r"\s*[—–-]\s*$", "", workplace))
                commitment = None
                commit_node = posting.select_one(".posting-categories .commitment")
                if commit_node is not None:
                    commitment = _clean(commit_node.get_text(" ", strip=True))
                location = None
                loc_node = posting.select_one(".posting-categories .location")
                if loc_node is not None:
                    location = _clean(loc_node.get_text(" ", strip=True))
                detail_url = BOARD_URL + f"/{identity}"
                rows.append({
                    "id": identity,
                    "title": title,
                    "url": detail_url,
                    "department": department,
                    "team": team,
                    "employment_type": commitment,
                    "workplace_type": workplace,
                    "location": location,
                })
        if not rows:
            raise ValueError(f"Dozee {snapshot_name} board contains no visible postings")
        return rows

    @staticmethod
    def _jsonld_posting(page_html):
        soup = BeautifulSoup(page_html or "", "lxml")
        scripts = soup.select('script[type="application/ld+json"]')
        if len(scripts) != 1:
            raise ValueError(
                f"Dozee exact detail page must carry exactly one JobPosting document ({len(scripts)})"
            )
        try:
            data = json.loads(scripts[0].string or scripts[0].get_text())
        except ValueError as exc:
            raise ValueError("Dozee exact detail JSON-LD is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            raise ValueError("Dozee exact detail document is not a JobPosting")
        return data

    @staticmethod
    def parse_detail(page_html, expected_id):
        data = Dozee._jsonld_posting(page_html)
        title = _clean(html.unescape(str(data.get("title") or "")))
        description_html = data.get("description")
        if not title:
            raise ValueError(f"posting {expected_id} detail has no title")
        if not isinstance(description_html, str) or not description_html.strip():
            raise ValueError(f"posting {expected_id} public detail description is empty")
        employer = None
        org = data.get("hiringOrganization")
        if isinstance(org, dict):
            employer = _clean(org.get("name"))
        # The JSON-LD @id is not always present; the observed URL identity is
        # the membership authority. Reject a mismatched explicit identifier.
        identifier = data.get("identifier")
        if isinstance(identifier, dict) and isinstance(identifier.get("value"), str):
            if identifier["value"].strip() != expected_id:
                raise ValueError(f"posting {expected_id} detail identifier changed: {identifier['value']!r}")
        locality = None
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
            loc = _clean(address.get("addressLocality"))
            if loc:
                localities.append(loc)
        if len(localities) == 1:
            locality = localities[0]
        elif len(localities) > 1:
            locality = "; ".join(localities)
        employment_type = _clean(data.get("employmentType"))
        date_posted = data.get("datePosted")
        if date_posted is not None and not (isinstance(date_posted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted.strip())):
            raise ValueError(f"posting {expected_id} detail datePosted is not YYYY-MM-DD: {date_posted!r}")
        if isinstance(date_posted, str):
            date_posted = date_posted.strip()
        # Rendered Apply contract: detail page exposes one /apply target.
        soup = BeautifulSoup(page_html or "", "lxml")
        apply_hrefs = sorted({
            anchor.get("href", "").strip()
            for anchor in soup.select("a.postings-btn[href]")
            if str(anchor.get("href", "")).strip()
        })
        expected_apply = apply_url_for(BOARD_URL, expected_id)
        if apply_hrefs != [expected_apply]:
            raise ValueError(
                f"posting {expected_id} detail Apply target differs: {apply_hrefs!r} != [{expected_apply!r}]"
            )
        # Header categories give the department/team string as rendered.
        header = soup.select_one(".posting-header")
        header_text = _clean(header.get_text(" ", strip=True)) if header is not None else None
        return {
            "title": title,
            "description_html": description_html,
            "date_posted": date_posted,
            "employment_type": employment_type,
            "locality": locality,
            "localities": localities,
            "employer": employer,
            "apply_url": expected_apply,
            "header_text": header_text,
            "jsonld": data,
        }

    async def _board_snapshot(self, board_url, label):
        response = await self.http.get(board_url)
        final = str(getattr(response, "url", board_url))
        if final.rstrip("/") != board_url.rstrip("/"):
            raise ValueError(f"Dozee {label} Lever board request redirected away from its published URL: {final}")
        return response.text

    async def _detail_snapshot(self, detail_url, identity):
        response = await self.http.get(detail_url)
        final = str(getattr(response, "url", detail_url))
        if final.rstrip("/") != detail_url.rstrip("/"):
            raise ValueError(f"Dozee exact detail page {detail_url} redirected to an uninspected route: {final}")
        return self.parse_detail(response.text, identity)

    def _make_job(self, row, detail, *, board_url, initial_row=None, membership_changed=False):
        identity = row["id"]
        if detail is None:
            description_html = ""
            description_text = ""
            date_posted = None
            employment_type = row.get("employment_type")
            locality = None
            employer = None
            apply_url = apply_url_for(board_url, identity)
            title = row["title"]
        else:
            description_html = detail["description_html"]
            description_text = text_html(description_html)
            date_posted = detail["date_posted"]
            employment_type = detail["employment_type"] or row.get("employment_type")
            locality = detail["locality"]
            employer = detail["employer"]
            apply_url = detail["apply_url"]
            title = row["title"]
            if detail["title"] != row["title"]:
                self.result.errors.append(
                    f"{identity}: Dozee detail title {detail['title']!r} differs from board title {row['title']!r}"
                )
            if employer != "Dozee":
                self.result.errors.append(
                    f"{identity}: Dozee posting {identity} has an unexpected hiring organization: {employer!r}"
                )
            if row.get("employment_type") and detail.get("employment_type") and row["employment_type"] != detail["employment_type"]:
                self.result.errors.append(
                    f"{identity}: Dozee board commitment {row['employment_type']!r} differs from detail employmentType {detail['employment_type']!r}"
                )
            if AI_DISCLAIMER_MARKER in description_text:
                self.result.errors.append(
                    f"{identity}: Dozee detail description leaked the generic hiring-AI page chrome"
                )
            if _location_differs(row.get("location"), locality):
                self.result.warnings.append(f"board_location_detail_locality_differ:{identity}")
        attachment_links = _attachment_links(description_html)
        salary = _salary_from_description(description_text)
        locations = [{"location": row.get("location"), "address": locality}]
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": native_job_url(board_url, identity),
                "rendered_apply_url": apply_url,
                "lever_board_url": board_url,
                "attachment_links": attachment_links,
                "salary_from_description": salary,
            },
        }
        if detail is not None:
            raw["detail"] = detail["jsonld"]
            raw["source_ui"]["detail_header_text"] = detail["header_text"]
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=title,
            url=native_job_url(board_url, identity),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="dozee.official_careers_lever_detail.jsonld_description",
            description_url=native_job_url(board_url, identity),
            locations=locations,
            department=row.get("department"),
            team=row.get("team"),
            employment_type=employment_type,
            workplace_type=row.get("workplace_type"),
            date_posted=date_posted,
            date_posted_raw=date_posted,
            date_updated=None,
            salary=salary,
            employer=employer,
        )
        job.description_text = description_text
        job.record_identity = f"dozee:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        if detail is None:
            job.warnings.append("detail_fetch_failed")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        self._assert_entry_url(entry_url)
        entry = await self.http.get(entry_url)
        self._assert_entry_url(str(entry.url))
        board_url = self.lever_board_url(entry.text, str(entry.url))

        snapshot_started = time.perf_counter()
        initial_html = await self._board_snapshot(board_url, "initial")
        initial_rows = self.parse_board(initial_html, "initial")
        initial_by_id = {row["id"]: row for row in initial_rows}

        # Representative detail: the first board-listed card, opened as a
        # browser would from the listing. Its response is reused for that job.
        first_identity = initial_rows[0]["id"]
        first_detail_url = native_job_url(board_url, first_identity)
        detail_started = time.perf_counter()
        first_detail = None
        detail_ui_error = None
        try:
            first_detail = await self._detail_snapshot(first_detail_url, first_identity)
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Dozee representative exact detail page {first_detail_url}: {detail_ui_error}")
        representative_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_html = await self._board_snapshot(board_url, "final")
            final_rows = self.parse_board(final_html, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_rows = initial_rows
            self.result.errors.append(
                "Dozee final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        snapshot_seconds = time.perf_counter() - snapshot_started
        final_by_id = {row["id"]: row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("dozee_official_listing_membership_changed_between_initial_and_final_snapshots")

        all_ids = list(dict.fromkeys([row["id"] for row in final_rows] + [row["id"] for row in initial_rows if row["id"] not in final_by_id]))
        details = {}
        if first_detail is not None:
            details[first_identity] = first_detail
        remaining_seconds = 0.0
        for identity in all_ids:
            if identity in details:
                continue
            detail_url = native_job_url(board_url, identity)
            detail_started_each = time.perf_counter()
            try:
                details[identity] = await self._detail_snapshot(detail_url, identity)
            except Exception as exc:
                self.result.errors.append(f"Dozee exact detail page {detail_url} for posting {identity}: {type(exc).__name__}: {exc}")
                details[identity] = None
            remaining_seconds += time.perf_counter() - detail_started_each
        for identity in all_ids:
            row = final_by_id.get(identity) or initial_by_id[identity]
            self.add(self._make_job(
                row, details.get(identity), board_url=board_url,
                initial_row=initial_by_id.get(identity) if initial_by_id.get(identity) != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        detail_mapping_seconds = remaining_seconds
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership or not final_snapshot_verified:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "lever_board_url": board_url,
            "membership_authority": "Dozee official /careers entry 'job openings' anchor to its published Lever board https://jobs.lever.co/dozee; unfiltered board page renders every open card in one page (filters are client-side query strings)",
            "listing_filter_behavior": "unfiltered board HTML is full membership; workplace/location/team/worktype popups only filter the same board rows",
            "ui_pagination": "None; single board page renders all visible cards grouped by department/team headers. No paged request or cursor was observed.",
            "department_filters": sorted({row.get("department") or "Other" for row in final_rows}),
            "initial_visible_ids": [row["id"] for row in initial_rows],
            "final_visible_ids": [row["id"] for row in final_rows],
            "final_snapshot_verified": final_snapshot_verified,
            "board_location_detail_locality_differs": sorted({
                identity for identity, row in final_by_id.items()
                if details.get(identity) and _location_differs(row.get("location"), details[identity]["locality"])
            }),
            "exact_native_detail_page_opened": first_detail_url,
            "representative_detail_jsonld_checked": first_detail is not None,
            "representative_detail_error": detail_ui_error,
            "description_source": "Each exact listed detail page's JobPosting JSON-LD description HTML; board Apply contract checked against /apply URLs.",
            "ats_wide_search_or_enumeration_used": False,
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
            "representative_detail_response_reused_for_job": first_detail is not None,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(representative_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = round(snapshot_seconds, 6)
        return self.result
