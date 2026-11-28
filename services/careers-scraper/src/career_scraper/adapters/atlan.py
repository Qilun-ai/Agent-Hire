"""Atlan's own JS careers listing and query-parameter job detail journey."""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
ASHBY_LISTING = re.compile(r"^/posting-api/job-board/([A-Za-z0-9_-]+)$")
FETCH_URL = re.compile(
    r"fetch\s*\(\s*([`'\"])(https://api\.ashbyhq\.com/posting-api/job-board/[^`'\"]+)\1"
)
CAREER_CARD_ROUTE = re.compile(
    r"^/careers/jobs/\?id=\$\{[A-Za-z_$][A-Za-z0-9_$]*\.id\}$"
)


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(entry_url, route_template, identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"Atlan official listing job ID is not a UUID: {identity!r}")
    match = re.search(r"\$\{([^}]+)\}", route_template)
    if not match or not CAREER_CARD_ROUTE.fullmatch(route_template):
        raise ValueError("Atlan company card route changed from its observed /careers/jobs/?id= template")
    return urljoin(entry_url, route_template.replace("${" + match.group(1) + "}", str(identity)))


def _salary_from_description(description):
    for line in (description or "").splitlines():
        value = line.strip()
        if re.match(r"Local Pay Range\s*:", value, re.I):
            salary = {"source_text": value, "source_field": "descriptionPlain"}
            if re.search(r"\bOTE\b", value, re.I):
                salary["semantics"] = (
                    "OTE total compensation, not base salary; preserve source label and text without conversion."
                )
            return salary
    return None


def _employment_type_label(value):
    return {
        "FullTime": "Full-time", "PartTime": "Part-time", "Intern": "Internship",
        "Contract": "Contract", "Temporary": "Temporary",
    }.get(value, value)


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class Atlan(Adapter):
    """Follow only the live components and board endpoint embedded by Atlan."""

    VERIFICATION_REVISION = "atlan.native-careers-list-and-detail-exact-board.1"
    CAREERS_HOST = "atlan.com"
    BOARD_HOST = "api.ashbyhq.com"
    EXPECTED_BOARD = "atlan"

    @staticmethod
    def _assert_atlan_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"atlan.com", "www.atlan.com"}:
            raise ValueError(f"Atlan company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Atlan company page asset has an unexpected path: {url}")

    async def _component_script(self, page_url, html, component_name):
        soup = BeautifulSoup(html, "lxml")
        candidates = [
            node.get("component-url")
            for node in soup.select("astro-island[component-url]")
            if component_name in node.get("component-url", "")
            and node.get("component-export") == "default"
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Atlan official {component_name} component is missing or ambiguous ({len(candidates)})"
            )
        script_url = urljoin(page_url, candidates[0])
        self._assert_atlan_url(script_url, path_prefix="/_astro/")
        response = await self.http.get(script_url)
        self._assert_atlan_url(response.url, path_prefix="/_astro/")
        return str(response.url), response.text

    @staticmethod
    def _listing_endpoint(script, label):
        urls = sorted({match[2] for match in FETCH_URL.finditer(script)})
        if len(urls) != 1:
            raise ValueError(f"Atlan {label} component does not expose exactly one embedded job-board fetch")
        url = urls[0]
        parsed = urlsplit(url)
        match = ASHBY_LISTING.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != Atlan.BOARD_HOST or not match
                or parsed.query or parsed.fragment):
            raise ValueError(f"Atlan {label} component exposes an unexpected job source: {url}")
        if match.group(1) != Atlan.EXPECTED_BOARD:
            raise ValueError(f"Atlan embedded job board changed from its inspected public tenant: {url}")
        return url

    @staticmethod
    def _native_route(script):
        templates = re.findall(r"href\s*:\s*`([^`]+)`", script)
        matches = [route for route in templates if CAREER_CARD_ROUTE.fullmatch(route)]
        if len(matches) != 1:
            raise ValueError("Atlan CareersJobListings no longer exposes the inspected exact job-card route")
        return matches[0]

    @staticmethod
    def _assert_listing_component(script, api_url):
        required = ("CareersJobListings", "isListed", "searchTerm", "department", "team", "title",
                    "job-listing-")
        if any(token not in script for token in required):
            raise ValueError("Atlan listing component no longer matches the inspected list/search/filter flow")
        if api_url not in script:
            raise ValueError("Atlan listing component no longer fetches its discovered job board")

    @staticmethod
    def _assert_detail_component(script, api_url):
        normalized = re.sub(r"\s+", "", script)
        required = (
            "CareersJobDetail", "newURLSearchParams(window.location.search).get(`id`)",
            "jobs.find(", "descriptionHtml", "applyUrl||", "jobUrl",
        )
        if any(token not in normalized for token in required) or api_url not in script:
            raise ValueError("Atlan native detail component no longer selects an exact ID from the embedded board")

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"Atlan {snapshot_name} embedded listing response has no jobs array")
        rows = payload["jobs"]
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, str) or not UUID.fullmatch(identity) for identity in ids):
            raise ValueError(f"Atlan {snapshot_name} listing contains a missing or invalid exact job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Atlan {snapshot_name} listing repeats a company-published ID")
        if any(not row.get("title") for row in rows):
            raise ValueError(f"Atlan {snapshot_name} listing contains an unidentified job")
        # This is the filter in Atlan's CareersJobListings component:
        # jobs.filter(job => job.isListed !== false). Search and department
        # filters are client-side and do not change the unfiltered membership.
        return [row for row in rows if row.get("isListed") is not False]

    @staticmethod
    def _ui_order(rows):
        departments = sorted({row.get("department") or "Other" for row in rows})
        return [row for department in departments for row in rows
                if (row.get("department") or "Other") == department]

    def _make_job(self, row, *, entry_url, route_template, api_url, detail_component_url,
                  initial_row=None, membership_changed=False):
        identity = row["id"]
        expected_job_url = f"https://jobs.ashbyhq.com/atlan/{identity}"
        expected_apply_url = expected_job_url + "/application"
        if row.get("jobUrl") != expected_job_url:
            raise ValueError(f"Atlan listed ID {identity} has an unexpected company-published jobUrl")
        apply_url = row.get("applyUrl") or row["jobUrl"]
        if apply_url not in {expected_apply_url, expected_job_url}:
            raise ValueError(f"Atlan listed ID {identity} has an unexpected company-published applyUrl")
        description_html = row.get("descriptionHtml") or ""
        description_text = text_html(description_html)
        plain_text = row.get("descriptionPlain")
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Atlan public detail descriptionHtml is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Atlan public detail description is implausibly short")
        if plain_text is None:
            self.result.errors.append(f"{identity}: Atlan public detail descriptionPlain is missing")
        if not isinstance(row.get("publishedAt"), str) or not row["publishedAt"].strip():
            # Date is source-only. The complete record keeps a null normalized
            # date rather than assigning a timestamp from its surrounding data.
            date_posted = None
        else:
            date_posted = row["publishedAt"]
        attachment_links = _job_attachment_links(description_html)
        salary = _salary_from_description(plain_text)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": native_job_url(entry_url, route_template, identity),
                "listed_job_url": row["jobUrl"],
                "rendered_apply_url": apply_url,
                "rendered_employment_type": _employment_type_label(row.get("employmentType")),
                "native_card_dom_id": f"job-listing-{identity}",
                "listing_api_url": api_url,
                "detail_component_url": detail_component_url,
                "attachment_links": attachment_links,
                "salary_from_description": salary,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=native_job_url(entry_url, route_template, identity),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="atlan.official_careers_embed.board_descriptionHtml",
            description_url=api_url,
            locations=[{"location": row.get("location"), "address": row.get("address")},
                       *(row.get("secondaryLocations") or [])],
            department=row.get("department"),
            team=row.get("team"),
            employment_type=row.get("employmentType"),
            workplace_type=row.get("workplaceType"),
            date_posted=date_posted,
            date_posted_raw=row.get("publishedAt"),
            date_updated=row.get("updatedAt"),
            salary=salary,
        )
        job.description_text = description_text
        job.record_identity = f"atlan:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"atlan.com", "www.atlan.com"}
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("Atlan dedicated flow must start from its official HTTPS /careers/ entry")
        entry = await self.http.get(entry_url)
        self._assert_atlan_url(entry.url, path_prefix="/careers")
        listing_component_url, listing_script = await self._component_script(
            str(entry.url), entry.text, "CareersJobListings"
        )
        api_url = self._listing_endpoint(listing_script, "CareersJobListings")
        self._assert_listing_component(listing_script, api_url)
        route_template = self._native_route(listing_script)

        initial_payload = await self._listing_snapshot(api_url, "initial")
        initial_rows = self._rows(initial_payload, "initial")
        initial_by_id = {row["id"]: row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Atlan official embedded listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result

        first_identity = self._ui_order(initial_rows)[0]["id"]
        exact_detail_page_url = native_job_url(str(entry.url), route_template, first_identity)
        detail_started = time.perf_counter()
        detail_component_url = None
        detail_ui_error = None
        try:
            detail_page = await self.http.get(exact_detail_page_url)
            self._assert_atlan_url(detail_page.url, path_prefix="/careers/jobs/")
            if urlsplit(str(detail_page.url)).query != f"id={first_identity}":
                raise ValueError("Atlan exact native detail route redirected to a different job ID or page")
            detail_component_url, detail_script = await self._component_script(
                str(detail_page.url), detail_page.text, "CareersJobDetail"
            )
            detail_api = self._listing_endpoint(detail_script, "CareersJobDetail")
            if detail_api != api_url:
                raise ValueError("Atlan native detail page fetches a different board from its official listing")
            self._assert_detail_component(detail_script, api_url)
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Atlan representative exact detail UI {exact_detail_page_url}: {detail_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_payload = await self._listing_snapshot(api_url, "final")
            final_rows = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "Atlan final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["id"]: row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("atlan_official_listing_membership_changed_between_initial_and_final_snapshots")

        # Keep final source records for stable IDs. On a moving listing, retain
        # rows seen in either snapshot and annotate stale initial-only rows so
        # the partial result does not silently discard usable evidence.
        ui_final_rows = self._ui_order(final_rows)
        ui_initial_rows = self._ui_order(initial_rows)
        all_ids = list(dict.fromkeys([row["id"] for row in ui_final_rows]
                                     + [row["id"] for row in ui_initial_rows]))
        changed_rows = [identity for identity in final_by_id.keys() & initial_by_id.keys()
                        if final_by_id[identity] != initial_by_id[identity]]
        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, entry_url=str(entry.url), route_template=route_template, api_url=api_url,
                detail_component_url=detail_component_url,
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            # An observed final total is still recorded, but the two captured
            # company-published snapshots disagree, so this cannot certify.
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_component_url": listing_component_url,
            "listing_api_url": api_url,
            "detail_component_url": detail_component_url,
            "native_job_route_template": route_template,
            "membership_authority": "Atlan CareersJobListings component embedded by its official /careers/ page; its exact unfiltered public board response, with only isListed=false rows excluded as the UI does",
            "listing_filter_behavior": "unfiltered board rows are filtered only by isListed !== false; department/search controls are client-side over title, team, and department",
            "ui_pagination": "No job pagination; the browser renders all API rows in sorted department groups. Nearby Page 1/Page 2/Next controls belong to a testimonial carousel.",
            "department_filters": ["All", *sorted({row.get("department") or "Other" for row in final_rows})],
            "initial_api_rows": len(initial_payload["jobs"]),
            "final_api_rows": len(final_payload["jobs"]),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [row["id"] for row in ui_initial_rows],
            "final_visible_ids": [row["id"] for row in ui_final_rows],
            "unlisted_ids_excluded_by_company_ui": sorted({
                str(row.get("id")) for row in initial_payload["jobs"] + final_payload["jobs"]
                if row.get("isListed") is False
            }),
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "exact_native_detail_page_opened": exact_detail_page_url,
            "representative_detail_component_reads_id_query_and_same_board": detail_ui_error is None,
            "representative_detail_ui_error": detail_ui_error,
            "description_source": "Each exact listed row's descriptionHtml and descriptionPlain; native detail component fetches the same board and selects only its query-string ID.",
            "source_has_structured_compensation_field": any("compensation" in row for row in final_rows),
            "salary_policy": "Exact Local Pay Range text found in a published full description is retained with its source label and semantics; other compensation remains null.",
            "job_attachment_links": {row["id"]: _job_attachment_links(row.get("descriptionHtml"))
                                     for row in final_rows if _job_attachment_links(row.get("descriptionHtml"))},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    async def _listing_snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Atlan {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Atlan {label} listing response is not JSON") from exc
        self._rows(payload, label)
        return payload
