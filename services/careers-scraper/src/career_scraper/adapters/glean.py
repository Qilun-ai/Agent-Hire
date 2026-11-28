"""Glean's official careers page and its embedded Greenhouse listing journey.

Observed journey (see companies/glean/research/):
- The company entry point is https://www.glean.com/careers (Webflow page
  whose inline script is patched into the document itself).
- Exactly one active inline <script> (no src) carries the company key
  `const company = "gleanwork"` and issues
  `fetch(`https://boards-api.greenhouse.io/v1/boards/${company}/jobs?content=true`)`
  on DOMContentLoaded. It maps `data.jobs` to
  `department = job.departments?.[0]?.name || "Other"`, renders all rows
  grouped by department into `.jobsection .jobcontainer` by cloning
  `.jobs-hidden-clones > #department`, `#dept-name` and `#listing`, and
  filters only client-side (title/location/department search plus the two
  `.blognew_select-filter_dropdown.is-career` dropdowns: All Departments /
  All Locations). There is no pagination: `meta.total` equals the row
  count and the component renders every row in one response.
- Four older Greenhouse variants sit inside `<!-- ... -->` HTML comments
  (departments index, department details, and two earlier jobs?content=true
  scripts) and are inactive. The only other board-like script is a Lever
  sample (`api.lever.co/v1/opportunities`) that targets a missing
  `#Jobs-container` element; it is not Glean membership.
- Each card's href is the row's `absolute_url`
  (`https://job-boards.greenhouse.io/gleanwork/jobs/<id>`). The bulk
  `content` field is byte-identical to the same job's board detail
  endpoint and to the `.job__description` body on its hosted page.
  Membership is therefore the unfiltered bulk response for exactly
  `gleanwork`; no other board, slug or ATS-wide enumeration is used.

This adapter follows only that shipped script and DOM: validate the entry,
derive the exact request, reconcile initial/final snapshots, verify the first
and last rendered cards' exact detail bodies (board API + hosted page), and
retain every row's full `content` HTML verbatim.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY_HOST = "www.glean.com"
BOARD = "gleanwork"
API_URL = "https://boards-api.greenhouse.io/v1/boards/gleanwork/jobs?content=true"
BOARD_DETAIL_BASE = "https://boards-api.greenhouse.io/v1/boards/gleanwork/jobs"
JOB_PAGE_BASE = "https://job-boards.greenhouse.io/gleanwork/jobs"

COMPANY_RE = re.compile(r"const\s+company\s*=\s*[\"']([^\"']+)[\"']")
TEMPLATE_RE = re.compile(
    r"`(https://boards-api\.greenhouse\.io/v1/boards/\$\{company\}/jobs\?content=true)`"
)
DIGITS_ID = re.compile(r"^\d+$")

SALARY_PATTERNS: list[tuple[str, str]] = [
    (r"The standard base salary range for this position is \$[^\.]*?annually\b", "base salary"),
    (r"The standard OTE \(on[-\u2011u2013]target earnings\) range for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard on[-\u2011]target earnings \(OTE\) range for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard on-target earnings \(OTE\) range for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard OTE range for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The on target earnings for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The on-target earnings for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard on-target earnings for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard on target earnings for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The OTE range for this role is \$[^\.]*?annually\b", "OTE"),
    (r"The on-target earnings \(OTE\) range for this position is \$[^\.]*?annually\b", "OTE"),
    (r"The standard range for this position is \$[^\.]*?annually\b", "standard range"),
    (r"The range for this position is \$[^\.]*?/ hour\b", "hourly rate"),
    (r"The hourly rate for this role is \$[^\.]*?\.", "hourly rate"),
    (r"The standard base salary range for this position depends on your leveling[^\.]*?\.", "base salary"),
]


def compact_body_hash(html: str) -> str:
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def board_detail_url(identity: str) -> str:
    if not DIGITS_ID.fullmatch(str(identity)):
        raise ValueError(f"Glean official listing job ID is not numeric: {identity!r}")
    return f"{BOARD_DETAIL_BASE}/{identity}?content=true"


def job_page_url(identity: str) -> str:
    if not DIGITS_ID.fullmatch(str(identity)):
        raise ValueError(f"Glean official listing job ID is not numeric: {identity!r}")
    return f"{JOB_PAGE_BASE}/{identity}"


def _salary_from_description(description_text: str | None):
    """Retain the verbatim published compensation sentence when clearly stated."""
    collapsed = re.sub(r"\s+", " ", description_text or "").strip()
    for pattern, kind in SALARY_PATTERNS:
        match = re.search(pattern, collapsed, flags=re.I)
        if match:
            source_text = match.group(0).strip()
            if "OTE" in source_text.upper() or "on target earnings" in source_text.lower() or "on-target earnings" in source_text.lower():
                semantics = (
                    "OTE / on-target earnings total compensation, not base salary; "
                    "preserve source label and text without conversion."
                )
            elif kind == "base salary":
                semantics = "Base salary range as published; preserve source text without conversion."
            elif kind == "hourly rate":
                semantics = "Hourly contract rate as published; preserve source text without conversion."
            else:
                semantics = "Standard compensation range as published; source does not specify composition."
            return {"source_text": source_text, "source_field": "content", "semantics": semantics}
    return None


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def locations_of(row):
    """Merge the published primary location label with the row's offices.

    The rendered card prints ``location.name`` verbatim, including
    semicolon-separated multi-site labels such as
    ``"Mountain View, CA; San Francisco, CA"``, while ``offices`` carries the
    same rows' office names plus their full city/region/country addresses
    (and sometimes a site the card label omits, e.g. ``Remote - US``).  Both
    are published fields of the same board row, so the primary label is kept
    first and every office (name and full ``location`` address) is merged in
    and de-duplicated by display name; an office address enriches a primary
    entry with the same name instead of repeating it.
    """
    merged: dict[str, dict] = {}

    def add(name, address):
        if not isinstance(name, str) or not name.strip():
            return
        name = name.strip()
        address = address.strip() if isinstance(address, str) and address.strip() else None
        entry = merged.get(name)
        if entry is None:
            merged[name] = {"name": name, "location": address}
        elif entry.get("location") is None and address is not None:
            entry["location"] = address

    primary = row.get("location")
    if isinstance(primary, dict):
        add(primary.get("name"), primary.get("location"))
    for office in row.get("offices") or []:
        if isinstance(office, dict):
            add(office.get("name"), office.get("location"))
    return list(merged.values())


def _department_of(row):
    departments = row.get("departments") or []
    for item in departments:
        if isinstance(item, dict) and item.get("name"):
            return str(item["name"])
    return "Other"


def _ui_order(rows):
    """Order rows as the shipped renderJobs groups them: department encounter order."""
    grouped: dict[str, list] = {}
    order: list[str] = []
    for row in rows:
        dept = _department_of(row)
        if dept not in grouped:
            grouped[dept] = []
            order.append(dept)
        grouped[dept].append(row)
    return [row for dept in order for row in grouped[dept]]


def _validate_row(row, snapshot_name):
    if not isinstance(row, dict):
        raise ValueError(f"Glean {snapshot_name} listing row is not an object")
    identity = row.get("id")
    if not isinstance(identity, int) or not DIGITS_ID.fullmatch(str(identity)):
        raise ValueError(f"Glean {snapshot_name} listing contains a missing or invalid exact job ID")
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Glean {snapshot_name} listing contains an unidentified job (ID {identity})")
    expected = job_page_url(str(identity))
    if row.get("absolute_url") != expected:
        raise ValueError(
            f"Glean {identity} absolute_url differs from the company-published job page: {row.get('absolute_url')}"
        )
    if row.get("company_name") != "Glean":
        raise ValueError(f"Glean {identity} company_name differs from the published employer: {row.get('company_name')}")
    content = row.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Glean {identity} published board row has no full description content")
    if len(text_html(content).strip()) < 100:
        raise ValueError(f"Glean {identity} published description is implausibly short")


class Glean(Adapter):
    """Follow only Glean's shipped careers component and its exact board."""

    VERIFICATION_REVISION = "glean.official-careers-greenhouse-list-and-content.2"

    @staticmethod
    def _parse_active_component(html: str):
        soup = BeautifulSoup(html, "lxml")
        candidates = []
        for node in soup.find_all("script"):
            if node.get("src"):
                continue
            text = node.get_text()
            if "boards-api.greenhouse.io" not in text or "/jobs?content=true" not in text:
                continue
            candidates.append(text)
        if len(candidates) != 1:
            raise ValueError(
                f"Glean official careers page must contain exactly one active Greenhouse listing script, found {len(candidates)}"
            )
        script = candidates[0]
        company_match = COMPANY_RE.search(script)
        if company_match is None:
            raise ValueError("Glean careers script no longer publishes a company key")
        company_key = company_match.group(1)
        if company_key != BOARD:
            raise ValueError(f"Glean careers script board changed from its inspected public tenant: {company_key!r}")
        template_match = TEMPLATE_RE.search(script)
        if template_match is None:
            raise ValueError("Glean careers script no longer exposes the inspected jobs?content=true fetch template")
        api_url = template_match.group(1).replace("${company}", company_key)
        if api_url != API_URL:
            raise ValueError(f"Glean careers script fetch changed from the inspected source: {api_url}")
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or parsed.hostname != "boards-api.greenhouse.io" or parsed.path != "/v1/boards/gleanwork/jobs" or parsed.query != "content=true":
            raise ValueError(f"Glean careers script exposes an unexpected job source: {api_url}")
        for token in ("renderJobs", "Search-Jobs", "All Departments", "All Locations", "DOMContentLoaded", "jobs?content=true"):
            if token not in script:
                raise ValueError(f"Glean listing script no longer matches the inspected list/search/filter flow (missing {token!r})")
        # DOM must still carry the templates the script clones.
        if soup.select_one(".jobsection .jobcontainer") is None:
            raise ValueError("Glean official careers page no longer renders its jobs container")
        clones = soup.select_one(".jobs-hidden-clones")
        if clones is None:
            raise ValueError("Glean official careers page no longer carries its hidden job templates")
        for template_id in ("#department", "#listing", "#dept-name"):
            if clones.select_one(template_id) is None:
                raise ValueError(f"Glean hidden template {template_id} is missing from the official page")
        if soup.select_one("#loading") is None or soup.select_one("#jobs-empty") is None or soup.select_one("#Search-Jobs") is None:
            raise ValueError("Glean official careers page no longer renders its loading/empty/search controls")
        dropdowns = soup.select(".blognew_select-filter_dropdown.is-career")
        if len(dropdowns) != 2:
            raise ValueError("Glean official careers page no longer renders its two department/location filter dropdowns")
        # The Lever sample script targets #Jobs-container, which must stay absent.
        if soup.select_one("#Jobs-container") is not None:
            raise ValueError("Glean careers DOM unexpectedly gained a Jobs-container node; re-inspect before collecting")
        return company_key, api_url, script, soup

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"Glean {snapshot_name} embedded listing response has no jobs array")
        rows = payload["jobs"]
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, int) or not DIGITS_ID.fullmatch(str(identity)) for identity in ids):
            raise ValueError(f"Glean {snapshot_name} listing contains a missing or invalid exact job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Glean {snapshot_name} listing repeats a company-published ID")
        for row in rows:
            _validate_row(row, snapshot_name)
        meta = payload.get("meta")
        total = meta.get("total") if isinstance(meta, dict) else None
        if total is not None and total != len(rows):
            raise ValueError(f"Glean {snapshot_name} board meta.total ({total}) differs from row count ({len(rows)})")
        return rows

    def _make_job(self, row, *, entry_url, initial_row=None, membership_changed=False):
        identity = str(row["id"])
        description_html = row.get("content") or ""
        description_text = text_html(description_html)
        job_url = row["absolute_url"]
        apply_url = row["absolute_url"]
        offices = row.get("offices") or []
        # `locations` merges the published primary `location.name` label (the
        # exact card text, possibly multi-site) with every office and its full
        # address. It must never be offices-only: 18 roles' card labels would
        # otherwise be absent (e.g. 4619072005 "San Francisco, CA" -> only
        # "Remote - US").
        locations = locations_of(row)
        departments = [dept.get("name") for dept in (row.get("departments") or []) if isinstance(dept, dict) and dept.get("name")]
        date_posted = row.get("first_published") if isinstance(row.get("first_published"), str) else None
        date_updated = row.get("updated_at") if isinstance(row.get("updated_at"), str) else None
        deadline = row.get("application_deadline") if isinstance(row.get("application_deadline"), str) else None
        requisition = row.get("requisition_id")
        salary = _salary_from_description(description_text)
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_card_href": job_url,
                "rendered_apply_url": apply_url,
                "apply_note": "Hosted Greenhouse job page; its Apply form posts to the same path. No separate applyUrl field is exposed.",
                "listing_api_url": API_URL,
                "board_detail_url": board_detail_url(identity),
                "hosted_job_page_url": job_page_url(identity),
                "department_display": _department_of(row),
                "location_display": (row.get("location") or {}).get("name"),
                "primary_location": row.get("location"),
                "offices": offices,
                "attachment_links": attachment_links,
                "salary_from_description": salary,
                "company_key": BOARD,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=job_url,
            job_id=identity,
            requisition_id=str(requisition) if requisition is not None else None,
            apply_url=apply_url,
            description=description_html,
            method="glean.official_careers.board_content",
            description_url=API_URL,
            locations=locations,
            department=departments[0] if departments else None,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=row.get("first_published"),
            date_updated=date_updated,
            valid_through=deadline,
            salary=salary,
            employer=row.get("company_name"),
            language=row.get("language"),
        )
        job.description_text = description_text
        job.record_identity = f"glean:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        if attachment_links:
            job.warnings.append("role_attachment_links_present_without_full_attachment_text")
        return job

    async def _listing_snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Glean {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Glean {label} listing response is not JSON") from exc
        self._rows(payload, label)
        return payload

    async def _representative_detail_check(self, initial_rows, ui_rows=None):
        """Verify the first and last rendered cards' exact board + hosted bodies."""
        representative = {}
        errors = []
        started = time.perf_counter()
        listed = ui_rows or initial_rows
        identities = list(dict.fromkeys([str(listed[0]["id"]), str(listed[-1]["id"])]))
        row_by_id = {str(item["id"]): item for item in initial_rows}
        for identity in identities:
            row = row_by_id.get(identity, {})
            try:
                detail_url = board_detail_url(identity)
                detail_response = await self.http.get(detail_url)
                if str(detail_response.url) != detail_url:
                    raise ValueError("representative board detail request redirected away")
                detail = detail_response.json()
                hosted_url = job_page_url(identity)
                hosted_response = await self.http.get(hosted_url)
                if str(hosted_response.url) != hosted_url:
                    raise ValueError("representative hosted detail request redirected away")
                soup = BeautifulSoup(hosted_response.text, "lxml")
                body = soup.select_one(".job__description")
                body_html = str(body) if body else ""
                record = {
                    "board_detail_url": detail_url,
                    "hosted_job_page_url": hosted_url,
                    "title_match": detail.get("title") == row.get("title"),
                    "content_match": detail.get("content") == row.get("content"),
                    "first_published_match": detail.get("first_published") == row.get("first_published"),
                    "updated_at_match": detail.get("updated_at") == row.get("updated_at"),
                    "hosted_body_hash_matches_listing": bool(body_html) and compact_body_hash(body_html) == compact_body_hash(row.get("content") or ""),
                    "hosted_body_length": len(text_html(body_html)) if body_html else 0,
                }
                representative[identity] = record
                if not (record["title_match"] and record["content_match"] and record["first_published_match"] and record["updated_at_match"] and record["hosted_body_hash_matches_listing"]):
                    errors.append(f"{identity}: board detail or hosted body differs from the listing row")
            except Exception as exc:
                errors.append(f"{identity}: {type(exc).__name__}: {exc}")
        seconds = time.perf_counter() - started
        return representative, errors, seconds

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if entry_parts.scheme != "https" or entry_parts.hostname not in {"www.glean.com", "glean.com"} or entry_parts.path.rstrip("/") != "/careers":
            raise ValueError("Glean dedicated flow must start from its official HTTPS /careers entry")
        entry_started = time.perf_counter()
        entry = await self.http.get(entry_url)
        entry_seconds = time.perf_counter() - entry_started
        entry_final = urlsplit(str(entry.url))
        if entry_final.scheme != "https" or entry_final.hostname not in {"www.glean.com", "glean.com"} or entry_final.path.rstrip("/") != "/careers":
            raise ValueError(f"Glean official careers entry redirected to an unexpected page: {entry.url}")
        company_key, api_url, script, _soup = self._parse_active_component(entry.text)

        initial_started = time.perf_counter()
        initial_payload = await self._listing_snapshot(api_url, "initial")
        initial_rows = self._rows(initial_payload, "initial")
        initial_seconds = time.perf_counter() - initial_started
        initial_by_id = {str(row["id"]): row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Glean official embedded listing returned zero jobs; native empty state requires independent review"
            )
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": str(entry.url),
                "listing_api_url": api_url,
                "company_key": company_key,
                "membership_authority": "Glean official /careers page active script fetching its unfiltered Greenhouse board gleanwork with content=true",
            })
            return self.result

        representative, detail_errors, detail_seconds = await self._representative_detail_check(
            initial_rows, _ui_order(initial_rows))
        if detail_errors:
            self.result.errors.extend(f"Glean representative exact detail {item}" for item in detail_errors)

        final_snapshot_verified = True
        final_started = time.perf_counter()
        try:
            final_payload = await self._listing_snapshot(api_url, "final")
            final_rows = self._rows(final_payload, "final")
            final_seconds = time.perf_counter() - final_started
        except Exception as exc:
            final_seconds = time.perf_counter() - final_started
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "Glean final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {str(row["id"]): row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("glean_official_listing_membership_changed_between_initial_and_final_snapshots")

        ui_final_rows = _ui_order(final_rows)
        ui_initial_rows = _ui_order(initial_rows)
        all_ids = list(dict.fromkeys([str(row["id"]) for row in ui_final_rows] + [str(row["id"]) for row in ui_initial_rows]))
        changed_rows = sorted(identity for identity in final_by_id.keys() & initial_by_id.keys() if final_by_id[identity] != initial_by_id[identity])

        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, entry_url=str(entry.url),
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        department_filters = sorted({_department_of(row) for row in final_rows})
        location_filters = sorted({str((row.get("location") or {}).get("name")) for row in final_rows if (row.get("location") or {}).get("name")})
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_api_url": api_url,
            "company_key": company_key,
            "membership_authority": "Glean official /careers page active script; its exact unfiltered public board response for gleanwork with content=true, with no rows excluded",
            "listing_filter_behavior": "unfiltered board rows rendered grouped by departments[0].name (or Other); department/location/search controls are client-side over title, location.name and department",
            "ui_pagination": "No pagination; the script fetches one board response with meta.total equal to the row count and renders all rows grouped by department",
            "department_filters": department_filters,
            "location_filters": location_filters,
            "location_mapping": "locations[0] is the published primary location.name label (verbatim, possibly multi-site); each offices[] entry (name + full location address) is merged and de-duplicated by name",
            "initial_api_rows": len(initial_payload["jobs"]),
            "final_api_rows": len(final_payload["jobs"]),
            "meta_total_initial": (initial_payload.get("meta") or {}).get("total"),
            "meta_total_final": (final_payload.get("meta") or {}).get("total"),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [str(row["id"]) for row in ui_initial_rows],
            "final_visible_ids": [str(row["id"]) for row in ui_final_rows],
            "initial_api_order_ids": [str(row["id"]) for row in initial_rows],
            "final_api_order_ids": [str(row["id"]) for row in final_rows],
            "source_rows_changed_between_snapshots": changed_rows,
            "representative_exact_detail_checks": representative,
            "representative_exact_detail_errors": detail_errors,
            "exact_board_detail_urls": [board_detail_url(identity) for identity in representative] if representative else [],
            "exact_hosted_detail_urls": [job_page_url(identity) for identity in representative] if representative else [],
            "description_source": "Each exact listed row's content HTML (byte-identical to the same job's board detail endpoint and hosted .job__description body for the two representative IDs).",
            "source_has_structured_compensation_field": False,
            "salary_policy": "Exact standard compensation sentences are retained verbatim from the description text with their source label and semantics; other compensation remains null.",
            "job_attachment_links": {str(row["id"]): _job_attachment_links(row.get("content")) for row in final_rows if _job_attachment_links(row.get("content"))},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "entry_fetch_seconds": round(entry_seconds, 6),
            "initial_listing_seconds": round(initial_seconds, 6),
            "collection_to_initial_listing_seconds": round(entry_seconds + initial_seconds, 6),
            "final_listing_seconds": round(final_seconds, 6),
            "initial_and_final_listing_requests": 2,
            "representative_detail_requests": 4,
        })
        self.result.timings["mapping_phase_seconds"] = round(mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["final_listing_phase_seconds"] = round(final_seconds, 6)
        self.result.timings["collection_phase_seconds"] = round(entry_seconds + initial_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
