"""Cyware's own Next.js careers listing: /api/careers (USA Greenhouse + India Zoho)."""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


GREENHOUSE_BOARD = "cyware"
ZOHO_CALLBACK = "https%3A%2F%2Fwww.cyware.com%2Fcareers%3Ftab%3Dindia%23open-positions"


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _zoho_slug(title):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(title or "").strip())
    slug = re.sub(r"^-+|-+$", "", slug)
    return slug


def zoho_job_url(job_id, title):
    """Construct the exact Zoho Recruit URL Cyware's JS builds for an India card."""
    identity = str(job_id or "").strip()
    if not identity:
        raise ValueError("Cyware Zoho job ID is missing")
    slug = _zoho_slug(title)
    if not slug:
        raise ValueError("Cyware Zoho job title is missing for URL construction")
    return (
        f"https://cyware.zohorecruit.com/jobs/Careers/{identity}/{slug}"
        f"?source=CareerSite&$list_callback_url={ZOHO_CALLBACK}"
    )


def greenhouse_detail_url(board, job_id):
    if board != GREENHOUSE_BOARD:
        raise ValueError(f"Cyware Greenhouse board changed from inspected tenant: {board!r}")
    return (
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"
    )


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _norm_text(value):
    import unicodedata

    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


class Cyware(Adapter):
    """Follow only Cyware's shipped /api/careers membership and its exact detail IDs."""

    VERIFICATION_REVISION = "cyware.official-careers-us-greenhouse-india-zoho-exact.1"
    ENTRY_HOSTS = {"cyware.com", "www.cyware.com"}

    @staticmethod
    def _assert_cyware_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Cyware.ENTRY_HOSTS:
            raise ValueError(f"Cyware company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Cyware company page asset has an unexpected path: {url}")

    @staticmethod
    def _script_candidates(entry_url, html):
        soup = BeautifulSoup(html, "lxml")
        candidates = []
        for node in soup.select("script[src]"):
            src = (node.get("src") or "").strip()
            if not src:
                continue
            if "/_next/static/immutable/chunks/" not in src and "/_next/static/" not in src:
                continue
            absolute = urljoin(str(entry_url), src)
            # Only Cyware's own Next.js chunks are valid component candidates.
            if urlsplit(absolute).hostname not in Cyware.ENTRY_HOSTS:
                continue
            if absolute not in candidates:
                candidates.append(absolute)
        if not candidates:
            raise ValueError("Cyware official careers page exposes no Next.js chunk candidates")
        return candidates

    @staticmethod
    def _is_listing_component(script):
        return (
            'fetch("/api/careers")' in script
            and "cyware.zohorecruit.com/jobs/Careers" in script
            and "usJobs" in script
            and "indiaJobs" in script
        )

    @staticmethod
    def _assert_listing_component(script):
        required = (
            'fetch("/api/careers")',
            "usJobs",
            "indiaJobs",
            "cyware.zohorecruit.com/jobs/Careers",
            "USA",
            "India",
            "open-positions",
        )
        if any(token not in script for token in required):
            raise ValueError("Cyware listing component no longer matches the inspected USA/India flow")
        if "absolute_url" not in script or "Client_Name" not in script:
            raise ValueError("Cyware listing component no longer maps exact USA/India card links")

    @staticmethod
    def _listing_api_url(entry_url):
        return urljoin(str(entry_url), "/api/careers")

    @staticmethod
    def _rows(payload, label):
        if not isinstance(payload, dict):
            raise ValueError(f"Cyware {label} listing response is not an object")
        us = payload.get("usJobs")
        india = payload.get("indiaJobs")
        if not isinstance(us, dict) or not isinstance(us.get("jobs"), list):
            raise ValueError(f"Cyware {label} listing response has no usJobs.jobs array")
        if not isinstance(india, dict) or not isinstance(india.get("data"), list):
            raise ValueError(f"Cyware {label} listing response has no indiaJobs.data array")
        us_rows = us["jobs"]
        india_rows = india["data"]
        us_ids = []
        for row in us_rows:
            if not isinstance(row, dict):
                raise ValueError(f"Cyware {label} US row is not an object")
            identity = row.get("id")
            if identity is None or str(identity).strip() == "":
                raise ValueError(f"Cyware {label} US listing contains a missing exact job ID")
            if not row.get("title"):
                raise ValueError(f"Cyware {label} US listing contains an unidentified job")
            us_ids.append(str(identity))
        if len(us_ids) != len(set(us_ids)):
            raise ValueError(f"Cyware {label} US listing repeats a company-published ID")
        india_ids = []
        for row in india_rows:
            if not isinstance(row, dict):
                raise ValueError(f"Cyware {label} India row is not an object")
            identity = row.get("id")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError(f"Cyware {label} India listing contains a missing exact job ID")
            if not (row.get("Title") or row.get("Posting_Title") or row.get("Job_Opening_Name")):
                raise ValueError(f"Cyware {label} India listing contains an unidentified job")
            india_ids.append(str(identity))
        if len(india_ids) != len(set(india_ids)):
            raise ValueError(f"Cyware {label} India listing repeats a company-published ID")
        # The company JS has no server-side pagination; indiaJobs.info documents
        # the single returned page. more_records must be false for completeness.
        info = india.get("info") or {}
        return us_rows, india_rows, info

    @staticmethod
    def _ui_order(us_rows, india_rows):
        """USA tab order then India Client_Name groups sorted as the JS does."""
        groups = {}
        for row in india_rows:
            name = (row.get("Client_Name") or {}).get("name") or "Other"
            groups.setdefault(name, []).append(row)
        ordered_india = []
        for name in sorted(groups):
            ordered_india.extend(groups[name])
        return list(us_rows), ordered_india

    def _make_us_job(self, row, *, listing_row=None, detail=None, detail_url=None,
                     api_url=None, membership_changed=False):
        identity = str(row["id"])
        listing_abs = row.get("absolute_url") or ""
        # The Greenhouse tenant is exposed by the exact company-published link.
        parsed = urlsplit(listing_abs)
        parts = [p for p in parsed.path.split("/") if p]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
            or len(parts) < 3
            or parts[0] != GREENHOUSE_BOARD
            or parts[1] != "jobs"
            or parts[2] != identity
        ):
            raise ValueError(f"Cyware US listed ID {identity} has unexpected absolute_url: {listing_abs}")
        if detail is not None:
            if str(detail.get("id")) != identity:
                raise ValueError(f"Cyware Greenhouse exact detail ID mismatch for {identity}")
            if detail.get("company_name") and detail.get("company_name") != "Cyware":
                raise ValueError(f"Cyware Greenhouse exact detail employer changed for {identity}")
            if detail.get("absolute_url") and detail.get("absolute_url") != listing_abs:
                raise ValueError(f"Cyware Greenhouse exact detail URL mismatch for {identity}")
            description_html = detail.get("content") or ""
            locations = [detail.get("location")] + list(detail.get("offices") or [])
            departments = detail.get("departments") or []
            department = departments[0].get("name") if departments and isinstance(departments[0], dict) else None
            date_posted = detail.get("first_published") or row.get("first_published")
            date_posted_raw = detail.get("first_published") or row.get("first_published")
            date_updated = detail.get("updated_at") or row.get("updated_at")
            requisition_id = detail.get("requisition_id") or row.get("requisition_id")
            title = detail.get("title") or row.get("title")
        else:
            description_html = ""
            locations = [row.get("location")]
            department = None
            date_posted = row.get("first_published")
            date_posted_raw = row.get("first_published")
            date_updated = row.get("updated_at")
            requisition_id = row.get("requisition_id")
            title = row.get("title")
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Cyware US Greenhouse exact detail content is empty")
        elif len(text_html(description_html).strip()) < 100:
            self.result.errors.append(f"{identity}: Cyware US Greenhouse exact detail description is implausibly short")
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "listed_absolute_url": listing_abs,
                "rendered_apply_url": listing_abs,
                "greenhouse_board": GREENHOUSE_BOARD,
                "greenhouse_detail_url": detail_url,
                "listing_api_url": api_url,
                "attachment_links": attachment_links,
            },
        }
        if detail is not None:
            raw["greenhouse_detail"] = detail
        if listing_row is not None and listing_row != row:
            raw["initial_listing"] = listing_row
        job = self.job(
            raw,
            title=title,
            url=listing_abs,
            job_id=identity,
            requisition_id=requisition_id,
            apply_url=listing_abs,
            description=description_html,
            method="cyware.official_careers_api.usJobs + greenhouse.exact_job.content",
            description_url=detail_url or api_url,
            locations=[loc for loc in locations if loc],
            department=department,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
            date_updated=date_updated,
            salary=None,
            language=detail.get("language") if detail else row.get("language"),
            employer="Cyware",
        )
        job.record_identity = f"cyware:us:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    def _make_india_job(self, row, *, listing_row=None, api_url=None, membership_changed=False):
        identity = str(row["id"])
        display_title = row.get("Title") or row.get("Posting_Title") or row.get("Job_Opening_Name")
        native_url = zoho_job_url(identity, display_title)
        description_html = row.get("Job_Description") or ""
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Cyware India listing Job_Description is empty")
        elif len(text_html(description_html).strip()) < 100:
            self.result.errors.append(f"{identity}: Cyware India listing Job_Description is implausibly short")
        client = row.get("Client_Name") if isinstance(row.get("Client_Name"), dict) else {}
        city = row.get("City")
        state = row.get("State")
        country = row.get("Country")
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": native_url,
                "rendered_apply_url": native_url,
                "rendered_title": display_title,
                "posting_title": row.get("Posting_Title"),
                "job_opening_name": row.get("Job_Opening_Name"),
                "client_name": client.get("name"),
                "listing_api_url": api_url,
                "attachment_links": attachment_links,
            },
        }
        if listing_row is not None and listing_row != row:
            raw["initial_listing"] = listing_row
        job = self.job(
            raw,
            title=display_title,
            url=native_url,
            job_id=identity,
            requisition_id=row.get("Job_Opening_ID"),
            apply_url=native_url,
            description=description_html,
            method="cyware.official_careers_api.indiaJobs.Job_Description",
            description_url=api_url,
            locations=[{"location": city, "address": state, "country": country}],
            department=client.get("name"),
            employment_type=row.get("Job_Type"),
            workplace_type=None,
            date_posted=row.get("Date_Opened"),
            date_posted_raw=row.get("Date_Opened"),
            date_updated=row.get("Modified_Time"),
            salary=None,
            qualifications=row.get("Required_Skills"),
            employer="Cyware",
        )
        job.record_identity = f"cyware:india:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname not in self.ENTRY_HOSTS
            or entry_parts.path.rstrip("/") != "/careers"
        ):
            raise ValueError("Cyware dedicated flow must start from its official HTTPS /careers entry")
        entry = await self.http.get(entry_url)
        self._assert_cyware_url(entry.url, path_prefix="/careers")
        if urlsplit(str(entry.url)).path.rstrip("/") != "/careers":
            raise ValueError("Cyware official entry redirected away from /careers")
        candidates = self._script_candidates(str(entry.url), entry.text)
        listing_matches = []
        for script_url in candidates:
            self._assert_cyware_url(script_url, path_prefix="/_next/static/")
            response = await self.http.get(script_url)
            self._assert_cyware_url(response.url, path_prefix="/_next/static/")
            if self._is_listing_component(response.text):
                listing_matches.append((str(response.url), response.text))
        if len(listing_matches) != 1:
            raise ValueError(
                f"Cyware official listing component is missing or ambiguous ({len(listing_matches)})"
            )
        listing_component_url, listing_script = listing_matches[0]
        self._assert_listing_component(listing_script)
        api_url = self._listing_api_url(str(entry.url))
        # The JS fetch is relative; the inspected absolute source is entry origin + /api/careers.
        if urlsplit(api_url).hostname not in self.ENTRY_HOSTS or urlsplit(api_url).path != "/api/careers":
            raise ValueError(f"Cyware listing API URL is unexpected: {api_url}")

        initial_payload = await self._listing_snapshot(api_url, "initial")
        initial_us, initial_india, initial_info = self._rows(initial_payload, "initial")
        initial_us_by_id = {str(row["id"]): row for row in initial_us}
        initial_india_by_id = {str(row["id"]): row for row in initial_india}
        ordered_us, ordered_india = self._ui_order(initial_us, initial_india)
        if not initial_us and not initial_india:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Cyware official /api/careers returned zero visible jobs; native empty state requires independent review"
            )
            return self.result

        # Hydrate exact US Greenhouse details for the initial membership only.
        detail_started = time.perf_counter()
        us_detail_by_id = {}
        us_detail_url_by_id = {}
        for row in ordered_us:
            identity = str(row["id"])
            detail_url = greenhouse_detail_url(GREENHOUSE_BOARD, identity)
            try:
                response = await self.http.get(detail_url)
                if str(response.url) != detail_url:
                    raise ValueError(f"Cyware Greenhouse exact detail redirected away from {detail_url}")
                try:
                    detail = response.json()
                except ValueError as exc:
                    raise ValueError(f"Cyware Greenhouse exact detail is not JSON for {identity}") from exc
                if not isinstance(detail, dict):
                    raise ValueError(f"Cyware Greenhouse exact detail is not an object for {identity}")
                us_detail_by_id[identity] = detail
                us_detail_url_by_id[identity] = detail_url
            except Exception as exc:
                self.result.errors.append(
                    f"Cyware US Greenhouse exact detail {detail_url}: {type(exc).__name__}: {exc}"
                )
        greenhouse_detail_seconds = time.perf_counter() - detail_started

        # Representative Zoho detail UI check: first India card in UI order.
        zoho_started = time.perf_counter()
        zoho_detail_url = None
        zoho_detail_error = None
        zoho_detail_match = None
        if ordered_india:
            zoho_row = ordered_india[0]
            zoho_identity = str(zoho_row["id"])
            zoho_display = zoho_row.get("Title") or zoho_row.get("Posting_Title") or zoho_row.get("Job_Opening_Name")
            zoho_detail_url = zoho_job_url(zoho_identity, zoho_display)
            try:
                zoho_page = await self.http.get(zoho_detail_url)
                if str(zoho_page.url) != zoho_detail_url:
                    raise ValueError("Cyware Zoho representative detail redirected away from its exact card URL")
                if zoho_page.status_code != 200:
                    raise ValueError(f"Cyware Zoho representative detail returned HTTP {zoho_page.status_code}")
                detail_text = self._zoho_detail_text(zoho_page.text)
                listing_text = (zoho_row.get("Job_Description") or "")
                zoho_detail_match = _norm_text(detail_text) == _norm_text(text_html(listing_text))
                if not zoho_detail_match:
                    raise ValueError("Cyware Zoho representative detail body text differs from listing Job_Description")
            except Exception as exc:
                zoho_detail_error = f"{type(exc).__name__}: {exc}"
                self.result.errors.append(f"Cyware representative Zoho detail {zoho_detail_url}: {zoho_detail_error}")
        zoho_detail_seconds = time.perf_counter() - zoho_started

        final_snapshot_verified = True
        final_payload = None
        try:
            final_payload = await self._listing_snapshot(api_url, "final")
            final_us, final_india, final_info = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_us, final_india, final_info = initial_us, initial_india, initial_info
            self.result.errors.append(
                "Cyware final official /api/careers snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_us_by_id = {str(row["id"]): row for row in final_us}
        final_india_by_id = {str(row["id"]): row for row in final_india}
        # Fetch details for any final-only US IDs so the partial result is complete.
        final_only_us = [identity for identity in final_us_by_id if identity not in us_detail_by_id]
        if final_only_us:
            extra_started = time.perf_counter()
            for identity in final_only_us:
                detail_url = greenhouse_detail_url(GREENHOUSE_BOARD, identity)
                try:
                    response = await self.http.get(detail_url)
                    if str(response.url) != detail_url:
                        raise ValueError(f"Cyware Greenhouse exact detail redirected away from {detail_url}")
                    detail = response.json()
                    if not isinstance(detail, dict):
                        raise ValueError(f"Cyware Greenhouse exact detail is not an object for {identity}")
                    us_detail_by_id[identity] = detail
                    us_detail_url_by_id[identity] = detail_url
                except Exception as exc:
                    self.result.errors.append(
                        f"Cyware US Greenhouse exact detail {detail_url}: {type(exc).__name__}: {exc}"
                    )
            greenhouse_detail_seconds = round(
                greenhouse_detail_seconds + (time.perf_counter() - extra_started), 6
            )

        final_ordered_us, final_ordered_india = self._ui_order(final_us, final_india)
        initial_combined = list(dict.fromkeys(
            [f"us:{str(r['id'])}" for r in ordered_us] + [f"india:{str(r['id'])}" for r in ordered_india]
        ))
        final_combined = list(dict.fromkeys(
            [f"us:{str(r['id'])}" for r in final_ordered_us] + [f"india:{str(r['id'])}" for r in final_ordered_india]
        ))
        changed_membership = final_snapshot_verified and set(initial_combined) != set(final_combined)
        if changed_membership:
            self.result.errors.append("cyware_official_listing_membership_changed_between_initial_and_final_snapshots")
        if final_snapshot_verified and (final_info.get("more_records") is True or initial_info.get("more_records") is True):
            self.result.errors.append("cyware_india_listing_reports_more_records; single-page membership is incomplete")

        all_us_ids = list(dict.fromkeys(
            [str(r["id"]) for r in final_ordered_us] + [str(r["id"]) for r in ordered_us]
        ))
        all_india_ids = list(dict.fromkeys(
            [str(r["id"]) for r in final_ordered_india] + [str(r["id"]) for r in ordered_india]
        ))
        mapping_started = time.perf_counter()
        for identity in all_us_ids:
            final_row = final_us_by_id.get(identity)
            initial_row = initial_us_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_us_job(
                row,
                listing_row=initial_row,
                detail=us_detail_by_id.get(identity),
                detail_url=us_detail_url_by_id.get(identity),
                api_url=api_url,
                membership_changed=changed_membership and identity not in final_us_by_id,
            ))
        for identity in all_india_ids:
            final_row = final_india_by_id.get(identity)
            initial_row = initial_india_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_india_job(
                row,
                listing_row=initial_row,
                api_url=api_url,
                membership_changed=changed_membership and identity not in final_india_by_id,
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_us) + len(final_india)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_component_url": listing_component_url,
            "listing_component_candidates": candidates,
            "listing_component_fetched": len(candidates),
            "listing_api_url": api_url,
            "membership_authority": "Cyware official /careers USA/India tabs rendered from its shipped fetch(\"/api/careers\"); usJobs.jobs (Greenhouse exact IDs) plus indiaJobs.data (Zoho exact IDs), no server pagination, no ATS board enumeration",
            "listing_filter_behavior": "unfiltered /api/careers rows; USA/India tabs are client-side over the same single response; no country/seniority filter applied by the collector",
            "ui_pagination": "No pagination; indiaJobs.info per_page=200 with more_records=false documents a single page; repeated ?page/?per_page probes return the same membership",
            "initial_us_ids": [str(r["id"]) for r in ordered_us],
            "initial_india_ids": [str(r["id"]) for r in ordered_india],
            "final_us_ids": [str(r["id"]) for r in final_ordered_us],
            "final_india_ids": [str(r["id"]) for r in final_ordered_india],
            "initial_visible_ids": initial_combined,
            "final_visible_ids": final_combined,
            "initial_us_api_rows": len(initial_us),
            "initial_india_api_rows": len(initial_india),
            "final_us_api_rows": len(final_us),
            "final_india_api_rows": len(final_india),
            "initial_india_info": initial_info,
            "final_india_info": final_info,
            "final_snapshot_verified": final_snapshot_verified,
            "greenhouse_board": GREENHOUSE_BOARD,
            "greenhouse_detail_urls": us_detail_url_by_id,
            "representative_zoho_detail_url": zoho_detail_url,
            "representative_zoho_detail_body_matches_listing": zoho_detail_match,
            "representative_zoho_detail_error": zoho_detail_error,
            "description_source_us": "Exact Greenhouse job endpoint content for each company-listed ID (boards-api.greenhouse.io/v1/boards/cyware/jobs/<id>?questions=true); listing absolute_url is the job/apply URL",
            "description_source_india": "Listing indiaJobs.data Job_Description plain text; representative Zoho detail HTML JSON body text matches whitespace-normalized listing text",
            "source_has_structured_compensation_field": False,
            "salary_policy": "No salary amounts exposed in listing or exact details; salary remains null for every job; CtcDetails and Greenhouse pay ranges are null/absent",
            "job_attachment_links": {
                f"us:{str(r['id'])}": _job_attachment_links((us_detail_by_id.get(str(r["id"])) or {}).get("content") or "")
                for r in final_us if _job_attachment_links((us_detail_by_id.get(str(r["id"])) or {}).get("content") or "")
            } | {
                f"india:{str(r['id'])}": _job_attachment_links(r.get("Job_Description") or "")
                for r in final_india if _job_attachment_links(r.get("Job_Description") or "")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(greenhouse_detail_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(zoho_detail_seconds, 6)
        self.result.timings["detail_mapping_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    async def _listing_snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Cyware {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Cyware {label} listing response is not JSON") from exc
        self._rows(payload, label)
        return payload

    @staticmethod
    def _zoho_detail_text(html):
        """Extract the Zoho detail Job_Description text from its embedded JSON payload."""
        import json

        match = re.search(r"JSON\.parse\('(\[.*?\])'\)", html or "", re.S)
        if not match:
            raise ValueError("Cyware Zoho detail page has no embedded jobs JSON payload")
        decoded = Cyware._js_single_quoted_decode(match.group(1))
        try:
            jobs = json.loads(decoded)
        except ValueError as exc:
            raise ValueError("Cyware Zoho detail embedded jobs payload is not valid JSON") from exc
        if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
            raise ValueError("Cyware Zoho detail embedded jobs payload is not a single job object")
        description_html = jobs[0].get("Job_Description") or ""
        return text_html(description_html)

    @staticmethod
    def _js_single_quoted_decode(value):
        out = []
        index = 0
        text = value or ""
        while index < len(text):
            char = text[index]
            if char != "\\":
                out.append(char)
                index += 1
                continue
            index += 1
            if index >= len(text):
                out.append("\\")
                break
            esc = text[index]
            index += 1
            if esc == "x" and index + 1 < len(text):
                try:
                    out.append(chr(int(text[index:index + 2], 16)))
                    index += 2
                    continue
                except ValueError:
                    pass
                out.append("x")
                continue
            if esc == "u" and index + 3 < len(text):
                try:
                    out.append(chr(int(text[index:index + 4], 16)))
                    index += 4
                    continue
                except ValueError:
                    pass
                out.append("u")
                continue
            escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
            out.append(escapes.get(esc, esc))
        return "".join(out)
