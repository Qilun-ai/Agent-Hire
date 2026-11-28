"""Aster DM Healthcare's company-published, global Oracle CX careers journey."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import gzip
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .base import Options
from .enterprise import Oracle
from ..models import Job, Source, now, text_html


class AsterDM(Oracle):
    """Use only the CX board linked by Aster DM Healthcare's current Careers page.

    The old generic run already saved exact individual Oracle detail responses
    for 628 current IDs. Reuse those exact records after the company-specific
    audit, and fetch details only for newly exposed IDs. An empty set of role
    sections remains explicitly unavailable even if the response has
    organization or corporate boilerplate.
    """

    VERIFICATION_REVISION = 1
    PAGE_SIZE = 24
    FACETS = (
        "WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
        "POSTING_DATES;FLEX_FIELDS;LOCATIONS"
    )
    ROLE_FIELDS = (
        "ExternalDescriptionStr",
        "ExternalResponsibilitiesStr",
        "ExternalQualificationsStr",
    )
    BODY_FIELDS = ROLE_FIELDS + (
        "OrganizationDescriptionStr",
        "CorporateDescriptionStr",
    )
    ARCHIVE_JOBS_PATH = Path(__file__).resolve().parents[3] / "output/full-linked-enterprise/jobs.jsonl"
    ARCHIVE_AUDIT_PATH = Path(__file__).resolve().parents[3] / "companies/aster-dm-healthcare/reuse-audit.json"
    CURRENT_DETAIL_CAPTURE_DIR = Path(__file__).resolve().parents[3] / "companies/aster-dm-healthcare/raw/current-details"
    CURRENT_DETAIL_CAPTURE_MANIFEST = Path(__file__).resolve().parents[3] / "companies/aster-dm-healthcare/current-detail-capture-manifest.json"
    CURRENT_PAGE_CAPTURE_DIR = Path(__file__).resolve().parents[3] / "companies/aster-dm-healthcare/raw/current-job-pages"
    CURRENT_PAGE_PILOT_DIR = Path(__file__).resolve().parents[3] / "companies/aster-dm-healthcare/raw/current-job-pages-pilot"

    def __init__(self, http, company, source: Source, options: Options):
        super().__init__(http, company, source, options)
        archive, stats = self._load_audited_archive()
        current_details, capture_stats = self._load_current_detail_captures()
        # Current-run cache must win over the historical source snapshot.
        self.options.cached_jobs = {**archive, **current_details, **self.options.cached_jobs}
        self._archive_stats = {**stats, "current_exact_detail_captures": capture_stats}
        self._id_to_cache_ref: dict[str, dict] = {}

    @staticmethod
    def _archive_root() -> Path:
        return Path(__file__).resolve().parents[3]

    def _record_replayed_response(self, *, url: str, body: bytes, content_type: str | None,
                                  status: int, captured_at: str, source_path: str) -> str | None:
        output = getattr(self.http, "output", None)
        if output is None:
            return None
        digest = hashlib.sha256(body).hexdigest()
        responses = Path(output) / "responses"
        responses.mkdir(parents=True, exist_ok=True)
        response_path = responses / f"{digest}.gz"
        if not response_path.exists():
            response_path.write_bytes(gzip.compress(body))
        requests_path = Path(output) / "requests.jsonl"
        if requests_path.exists():
            for line in requests_path.open(encoding="utf-8"):
                try:
                    prior = json.loads(line)
                except ValueError:
                    continue
                if prior.get("url") == url and prior.get("sha256") == digest:
                    return f"responses/{digest}.gz"
        record = {
            "url": url,
            "final_url": url,
            "method": "GET",
            "request_body": None,
            "status": status,
            "sha256": digest,
            "content_type": content_type,
            "redirects": [],
            "fetched_at": captured_at,
            "replayed_from": source_path,
            "capture_timestamp_basis": "local research response-file mtime UTC",
        }
        with requests_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return f"responses/{digest}.gz"

    @staticmethod
    def _read_capture_headers(path: Path) -> dict[str, str]:
        values = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if ": " in line:
                    key, value = line.split(": ", 1)
                    values[key] = value
        return values

    @classmethod
    def _capture_timestamp(cls, path: Path, manifest_row: dict | None = None) -> str:
        if manifest_row and manifest_row.get("captured_at"):
            return str(manifest_row["captured_at"])
        from datetime import datetime, timezone
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()

    def _load_current_detail_captures(self) -> tuple[dict[str, Job], dict]:
        manifest_path = Path(self.CURRENT_DETAIL_CAPTURE_MANIFEST)
        capture_dir = Path(self.CURRENT_DETAIL_CAPTURE_DIR)
        audit_path = Path(self.ARCHIVE_AUDIT_PATH)
        if not manifest_path.is_file() or not audit_path.is_file():
            return {}, {"loaded": False, "count": 0, "reason": "no_current_detail_capture_manifest"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        new_ids = {str(job_id) for job_id in audit.get("current_ids_not_in_previous", [])}
        manifest_by_id = {str(row["id"]): row for row in manifest.get("captured_ids", [])}
        cache: dict[str, Job] = {}
        replayed = 0
        invalid = 0
        for job_id in sorted(new_ids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value)):
            capture = manifest_by_id.get(job_id)
            json_path = capture_dir / f"{job_id}.json"
            if not capture or not json_path.is_file():
                continue
            body = json_path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            headers = self._read_capture_headers(json_path.with_suffix(".headers"))
            url = headers.get("url")
            status = headers.get("status")
            content_type = headers.get("content-type")
            expected_detail_url = f"{urlsplit(self.source.url).scheme}://{urlsplit(self.source.url).netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            parsed = urlsplit(url or "")
            finder = parse_qs(parsed.query).get("finder", [""])[0]
            if (
                digest != capture.get("sha256")
                or digest != headers.get("sha256")
                or status != "200"
                or parsed.scheme != "https"
                or parsed.netloc != urlsplit(self.source.url).netloc
                or parsed.path != urlsplit(expected_detail_url).path
                or finder != f'ById;Id="{job_id}",siteNumber=CX'
            ):
                invalid += 1
                continue
            try:
                payload = json.loads(body)
                rows = payload.get("items") or []
            except (ValueError, AttributeError):
                invalid += 1
                continue
            if len(rows) != 1 or str(rows[0].get("Id")) != job_id or rows[0].get("Title") != capture.get("title"):
                invalid += 1
                continue
            detail = rows[0]
            job_url = f"https://{parsed.netloc}/hcmUI/CandidateExperience/en/sites/CX/job/{job_id}"
            role_html = [str(detail.get(field) or "") for field in self.ROLE_FIELDS]
            role_available = any(text_html(value) for value in role_html)
            captured_at = self._capture_timestamp(json_path, capture)
            try:
                evidence_path = str(json_path.relative_to(self._archive_root()))
            except ValueError:
                evidence_path = str(json_path)
            output_response_path = self._record_replayed_response(
                url=url,
                body=body,
                content_type=content_type,
                status=int(status),
                captured_at=captured_at,
                source_path=evidence_path,
            )
            job = Job(
                company=self.company.name,
                source="oracle",
                title=detail.get("Title") or "",
                url=job_url,
                job_id=job_id,
                requisition_id=str(detail["RequisitionId"]) if detail.get("RequisitionId") else None,
                date_posted=detail.get("ExternalPostedStartDate"),
                date_posted_raw=(detail.get("ExternalPostedStartDate") or "")[:10] or None,
                valid_through=detail.get("ExternalPostedEndDate"),
                locations=self._location_values(detail),
                department=detail.get("Department") or detail.get("Category"),
                team=detail.get("JobFunction") or detail.get("JobFamily"),
                employment_type=detail.get("JobSchedule") or detail.get("JobType"),
                workplace_type=detail.get("WorkplaceTypeCode") or detail.get("WorkplaceType") or None,
                qualifications=detail.get("ExternalQualificationsStr") or None,
                responsibilities=detail.get("ExternalResponsibilitiesStr") or None,
                education=detail.get("StudyLevel"),
                language=detail.get("ContentLocale"),
                employer=detail.get("LegalEmployer"),
                description_html=self._body_from_detail(detail) if role_available else "",
                description_method="oracle.external_full_fields",
                description_source_url=url,
                description_unavailable=not role_available,
                description_verified=False,
                fetched_at=captured_at,
                raw={
                    "detail": detail,
                    "detail_response_evidence": {
                        "url": url,
                        "status": int(status),
                        "sha256": digest,
                        "source_capture_path": evidence_path,
                        "output_response_path": output_response_path,
                        "captured_at": captured_at,
                        "captured_at_basis": capture.get("captured_at_basis"),
                    },
                    "source_detail_scope": "exact ID from current company-published CX All Jobs membership",
                },
            )
            if not role_available:
                job.warnings.extend(["source_description_missing", "description_not_full_field"])
            else:
                job.description_text = text_html(job.description_html)
                job.description_sha256 = hashlib.sha256(job.description_text.encode()).hexdigest()
                job.description_verified = len(job.description_text) >= 100
            page_evidence = self._saved_native_page_evidence(job)
            if page_evidence:
                job.raw["saved_native_page_evidence"] = page_evidence
                page_path = self._archive_root() / page_evidence["path"]
                page_headers = self._read_capture_headers(page_path.with_suffix(".headers"))
                page_body = page_path.read_bytes()
                page_captured = self._capture_timestamp(page_path)
                page_evidence["output_response_path"] = self._record_replayed_response(
                    url=page_headers["url"],
                    body=page_body,
                    content_type=page_headers.get("content-type"),
                    status=int(page_headers.get("status", "200")),
                    captured_at=page_captured,
                    source_path=page_evidence["path"],
                )
            cache[job_url] = job
            replayed += 1
        return cache, {"loaded": True, "count": len(cache), "replayed_to_output": replayed, "invalid": invalid,
                       "ids": sorted(cache[job_url].job_id for job_url in cache)}

    @classmethod
    def _load_audited_archive(cls) -> tuple[dict[str, Job], dict]:
        jobs_path = Path(cls.ARCHIVE_JOBS_PATH)
        audit_path = Path(cls.ARCHIVE_AUDIT_PATH)
        sqlite_path = jobs_path.with_name("jobs.sqlite3")
        if not (sqlite_path.is_file() or jobs_path.is_file()) or not audit_path.is_file():
            return {}, {"loaded": False, "reason": "no_audited_archive"}

        audit = json.loads(audit_path.read_text())
        if audit.get("issues"):
            return {}, {"loaded": False, "reason": "archive_audit_has_issues"}
        audit_by_id = {
            str(row["id"]): row for row in audit.get("rows", [])
            if row.get("exact_identity") and row.get("exact_saved_response")
        }
        cache: dict[str, Job] = {}
        role_complete = role_empty = 0
        skipped = 0

        if sqlite_path.is_file():
            # The historical run has many companies. Use the old company index
            # rather than parsing the entire multi-company JSONL export.
            connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                records = (json.loads(raw) for (raw,) in connection.execute(
                    "SELECT data FROM jobs WHERE company=?", ("Aster DM Healthcare",)
                ))
                cached_records = list(records)
            finally:
                connection.close()
        else:
            cached_records = [
                json.loads(line) for line in jobs_path.open(encoding="utf-8")
                if json.loads(line).get("company") == "Aster DM Healthcare"
            ]

        for data in cached_records:
            if data.get("company") != "Aster DM Healthcare":
                continue
            data.pop("record_id", None)
            job_id = str(data.get("job_id") or "")
            audit_row = audit_by_id.get(job_id)
            raw = data.get("raw") or {}
            detail = raw.get("detail") or {}
            expected_url = f"https://hcdtgccprod-iayeqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/{job_id}"
            if (
                not audit_row
                or not job_id
                or str(detail.get("Id")) != job_id
                or detail.get("Title") != data.get("title")
                or data.get("url") != expected_url
            ):
                skipped += 1
                continue

            if bool(data.get("description_verified")):
                # The audit establishes that the archived normalized body is
                # exactly the body fields in this ID's saved 200 response.
                if not audit_row.get("raw_section_text_match") or not audit_row.get("source_url"):
                    skipped += 1
                    continue
                role_complete += 1
                detail_url = data.get("description_source_url")
                if not cls._exact_detail_url(detail_url, job_id):
                    skipped += 1
                    continue
                job = Job(**data)
                # The old normalization placed QUALIFICATIONS before
                # RESPONSIBILITIES. Build the page's observed section order
                # from the preserved exact fields, retaining the original raw
                # response and its fetched timestamp.
                job.description_html = cls._body_from_detail(detail)
                job.description_text = text_html(job.description_html)
                job.description_source_url = detail_url
                job.description_method = "oracle.external_full_fields"
                job.qualifications = detail.get("ExternalQualificationsStr") or None
                job.responsibilities = detail.get("ExternalResponsibilitiesStr") or None
                job.description_unavailable = False
                job.description_verified = True
            else:
                # All three external role fields were present and empty in the
                # exact saved response. Do not expose stale boilerplate copied
                # into a prior normalized description as if it described the
                # role. Keep the complete response in raw.detail.
                if not cls._exact_detail_url(data.get("description_source_url"), job_id):
                    skipped += 1
                    continue
                if not all(field in detail for field in cls.ROLE_FIELDS):
                    skipped += 1
                    continue
                if any(text_html(str(detail.get(field) or "")) for field in cls.ROLE_FIELDS):
                    skipped += 1
                    continue
                role_empty += 1
                job = Job(**data)
                old_text = job.description_text or ""
                job.raw = dict(job.raw)
                job.raw["archived_normalized_body_excluded"] = {
                    "reason": "prior body contained no external role section",
                    "sha256": hashlib.sha256(old_text.encode()).hexdigest() if old_text else None,
                    "archive_record": "output/full-linked-enterprise/jobs.jsonl",
                }
                job.description_html = ""
                job.description_text = ""
                job.description_sha256 = None
                job.description_verified = False
                job.description_unavailable = True
                job.description_method = "oracle.external_full_fields"
                job.warnings = [
                    warning for warning in job.warnings
                    if warning not in {"short_or_missing_description", "no_full_description_provenance"}
                ]
                for warning in ("source_description_missing", "description_not_full_field"):
                    if warning not in job.warnings:
                        job.warnings.append(warning)
                job.qualifications = None
                job.responsibilities = None

            detail_url = data.get("description_source_url")
            parsed_query = parse_qs(urlsplit(detail_url).query)
            request_hash = audit_row.get("request_hash")
            job.raw = dict(job.raw)
            job.raw["archive_reuse"] = {
                "origin": "output/full-linked-enterprise",
                "audit": "companies/aster-dm-healthcare/reuse-audit.json",
                "response_path": f"output/full-linked-enterprise/responses/{request_hash}.gz",
                "response_sha256": request_hash,
                "detail_request_url": detail_url,
                "detail_finder": parsed_query.get("finder", [None])[0],
                "original_fetched_at": data.get("fetched_at"),
                "exact_id_title_response_and_current_date_audited": True,
            }
            page_evidence = cls._saved_native_page_evidence(job)
            if page_evidence:
                job.raw["saved_native_page_evidence"] = page_evidence
            cache[job.url] = job

        return cache, {
            "loaded": True,
            "snapshot": "output/full-linked-enterprise",
            "audit": "companies/aster-dm-healthcare/reuse-audit.json",
            "archived_role_bodies": role_complete,
            "archived_empty_role_details": role_empty,
            "skipped_records": skipped,
        }

    @staticmethod
    def _exact_detail_url(url: str | None, job_id: str) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urlsplit(url)
        finder = parse_qs(parsed.query).get("finder", [""])[0]
        return (
            parsed.scheme == "https"
            and parsed.netloc == "hcdtgccprod-iayeqy.fa.ocs.oraclecloud.com"
            and parsed.path.endswith("/recruitingCEJobRequisitionDetails")
            and f'ById;Id="{job_id}",siteNumber=CX' == finder
        )

    @classmethod
    def _body_from_detail(cls, detail: dict) -> str:
        parts = []
        labels = {
            "ExternalDescriptionStr": "JOB DESCRIPTION",
            "ExternalResponsibilitiesStr": "JOB RESPONSIBILITIES",
            "ExternalQualificationsStr": "QUALIFICATIONS",
            "CorporateDescriptionStr": "ABOUT US",
        }
        for field in cls.BODY_FIELDS:
            value = detail.get(field)
            if isinstance(value, str) and value.strip():
                label = labels.get(field)
                if label:
                    parts.append(f"<section><h2>{label}</h2>{value}</section>")
                else:
                    # Preserve the full organization field in the exact order;
                    # its visible heading is not inferred from an API key.
                    parts.append(f'<section data-source-field="{field}">{value}</section>')
        return "\n".join(parts)

    @classmethod
    def _saved_native_page_evidence(cls, job: Job) -> dict | None:
        root = cls._archive_root()
        candidates = [
            Path(cls.CURRENT_PAGE_CAPTURE_DIR) / f"{job.job_id}.html",
            Path(cls.CURRENT_PAGE_PILOT_DIR) / f"{job.job_id}.html",
        ]
        for path in candidates:
            if not path.is_file():
                continue
            header_path = path.with_suffix(".headers")
            headers = header_path.read_text() if header_path.is_file() else ""
            url_match = re.search(r"^url: (.+)$", headers, re.M)
            status_match = re.search(r"^status: (\d+)$", headers, re.M)
            sha_match = re.search(r"^sha256: ([0-9a-f]{64})$", headers, re.M)
            expected_url = f"https://hcdtgccprod-iayeqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/{job.job_id}"
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if (not url_match or url_match.group(1) != expected_url or not status_match
                    or status_match.group(1) != "200" or not sha_match or sha_match.group(1) != digest):
                continue
            soup = BeautifulSoup(body, "html.parser")
            title_meta = soup.find("meta", attrs={"property": "og:title"})
            description_meta = soup.find("meta", attrs={"property": "og:description"})
            title = title_meta.get("content", "") if title_meta else ""
            if title and title != job.title:
                continue
            preview = description_meta.get("content", "") if description_meta else ""
            static_body = text_html(soup.body.get_text(" ", strip=True) if soup.body else "")
            return {
                "url": expected_url,
                "path": str(path.relative_to(root)),
                "sha256": digest,
                "status": 200,
                "captured_at": cls._capture_timestamp(path),
                "og_title": title or None,
                "og_description_preview": preview,
                "og_description_chars": len(preview),
                "og_description_truncated": bool(re.search(r"(?:\.\.\.|…)$", preview.strip())),
                "static_body_chars": len(static_body),
                "rendered_role_body_used": False,
            }
        return None

    @staticmethod
    def _location_values(row: dict) -> list:
        values = []
        primary = row.get("PrimaryLocation")
        if primary:
            values.append(primary)
        for location in row.get("secondaryLocations") or []:
            if isinstance(location, dict):
                value = location.get("LocationName") or location.get("name") or location.get("Name")
                values.append(value if value else location)
            elif location:
                values.append(location)
        return values

    def _job_from_listing(self, row: dict, listing_url: str) -> Job:
        job_id = str(row.get("Id") or "")
        url = f"https://hcdtgccprod-iayeqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/{job_id}"
        # `source` records the public ATS, while the source report uses the
        # company-specific `aster_dm` adapter. This keeps the exact historic
        # Oracle record identity reusable without conflating the two labels.
        job = self.job(
            row,
            title=row.get("Title") or "",
            job_id=job_id,
            url=url,
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            locations=self._location_values(row),
            department=row.get("JobFamily") or row.get("Category"),
            employment_type=row.get("JobSchedule") or row.get("JobType"),
            workplace_type=row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or None,
            language=row.get("Language"),
        )
        job.source = "oracle"
        job.raw = {"current_listing": row}
        job.raw["listing_request_url"] = listing_url
        job.raw["listing_site_number"] = "CX"
        return job

    def _prepare_cache_for_listing(self):
        live_urls = {job.url for job in self.result.jobs}
        for url in list(self.options.cached_jobs):
            if url not in live_urls:
                continue
            current = next(job for job in self.result.jobs if job.url == url)
            cached = self.options.cached_jobs[url]
            detail = (cached.raw or {}).get("detail") or {}
            current_day = str(current.date_posted_raw or "")[:10]
            cached_day = str(cached.date_posted or "")[:10]
            exact = (
                cached.job_id == current.job_id
                and cached.title == current.title
                and str(detail.get("Id")) == str(current.job_id)
                and detail.get("Title") == current.title
                and (not current_day or not cached_day or current_day == cached_day)
            )
            if not exact:
                self.options.cached_jobs.pop(url, None)
                continue

            # Keep the newest All Jobs membership row with the historic exact
            # detail response and original body fetched_at.
            cached.raw = dict(cached.raw)
            cached.raw["previous_listing"] = cached.raw.get("listing")
            cached.raw["current_listing"] = current.raw.get("current_listing")
            cached.raw["listing_request_url"] = current.raw.get("listing_request_url")
            cached.raw["listing_site_number"] = "CX"
            cached.locations = current.locations
            cached.date_posted_raw = current.date_posted_raw
            if cached.employment_type is None:
                cached.employment_type = current.employment_type
            if cached.department is None:
                cached.department = current.department
            if cached.workplace_type is None:
                cached.workplace_type = current.workplace_type
            cached.record_identity = current.key

    async def _facet_probe(self, base: str, site: str):
        params = {
            "onlyData": "true",
            "expand": "flexFieldsFacet.values",
            "finder": f"findReqs;siteNumber={site},facetsList={self.FACETS},limit={self.PAGE_SIZE}",
        }
        data = await self.http.json(base, params=params)
        items = data.get("items") or []
        if len(items) != 1 or str(items[0].get("SiteNumber")) != site:
            raise ValueError("Aster CX facet response did not identify the published site")
        return items[0]

    async def run(self):
        parsed = urlsplit(self.source.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        site_parts = parsed.path.split("/sites/")
        site = site_parts[1].split("/")[0] if len(site_parts) == 2 else ""
        if origin != "https://hcdtgccprod-iayeqy.fa.ocs.oraclecloud.com" or site != "CX":
            self.result.errors.append("official_aster_cx_site_identity_mismatch")
            return self.result

        base = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        facet = await self._facet_probe(base, site)
        self.result.expected_total = int(facet.get("TotalJobsCount"))
        self.result.metadata["native_facet_response"] = {
            "site_number": site,
            "total_jobs_count": self.result.expected_total,
            "all_jobs_label_from_parent_browser_reference": 633,
            "parent_browser_reference": "companies/aster-dm-healthcare/browser-reference-parent.json",
        }
        if self.result.expected_total != 633:
            # The count is intentionally a source observation, not a frozen
            # site constant. A changed site requires a new membership review.
            self.result.warnings.append("aster_cx_total_changed_since_inspected_native_ui")

        offset = 0
        seen_ids: set[str] = set()
        initial_total = self.result.expected_total
        row_expand = "requisitionList.workLocation,requisitionList.secondaryLocations"
        while self.page_guard():
            finder = (
                f"findReqs;siteNumber={site},limit={self.PAGE_SIZE},offset={offset},"
                "sortBy=POSTING_DATES_DESC"
            )
            params = {
                "onlyData": "true",
                "expand": row_expand,
                "finder": finder,
                "limit": self.PAGE_SIZE,
                "offset": offset,
            }
            response = await self.http.request("GET", base, params=params)
            listing_url = str(response.url)
            try:
                data = response.json()
            except ValueError as exc:
                raise ValueError(f"Aster CX listing page at offset {offset} was not JSON") from exc
            items = data.get("items") or []
            if len(items) != 1:
                self.result.errors.append(f"aster_cx_listing_page_shape_invalid_at_offset_{offset}")
                break
            block = items[0]
            if str(block.get("SiteNumber")) != site:
                self.result.errors.append(f"aster_cx_listing_site_mismatch_at_offset_{offset}")
                break
            observed_total = int(block.get("TotalJobsCount", -1))
            if observed_total != initial_total:
                self.result.errors.append(
                    f"aster_cx_total_changed_during_listing:{initial_total}->{observed_total}"
                )
                break
            rows = block.get("requisitionList")
            if not isinstance(rows, list):
                self.result.errors.append(f"aster_cx_requisition_list_missing_at_offset_{offset}")
                break
            if not rows:
                self.result.errors.append(f"aster_cx_pagination_stopped_at_offset_{offset}")
                break
            before = len(self.result.jobs)
            for row in rows:
                job_id = str(row.get("Id") or "")
                if not job_id or not row.get("Title"):
                    self.result.errors.append(f"aster_cx_listing_identity_missing_at_offset_{offset}")
                    continue
                if job_id in seen_ids:
                    self.result.errors.append(f"aster_cx_duplicate_public_id:{job_id}")
                    continue
                seen_ids.add(job_id)
                self.add(self._job_from_listing(row, listing_url))
            if len(self.result.jobs) == initial_total:
                self.result.listing_complete = True
                break
            if len(self.result.jobs) == before:
                self.result.errors.append(f"aster_cx_no_new_ids_at_offset_{offset}")
                break
            offset += len(rows)

        self.result.expected_total = initial_total
        self.result.metadata["listing_membership"] = {
            "source_total": initial_total,
            "collected_ids": len(seen_ids),
            "page_size": self.PAGE_SIZE,
            "first_id": next(iter(self.result.jobs)).job_id if self.result.jobs else None,
            "last_id": self.result.jobs[-1].job_id if self.result.jobs else None,
            "top_level_has_more_used_as_end_condition": False,
            "page_order": "POSTING_DATES_DESC",
        }
        if len(self.result.jobs) != initial_total:
            self.result.listing_complete = False

        self._prepare_cache_for_listing()

        async def detail(job: Job) -> Job:
            api = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            params = {
                "onlyData": "true",
                "expand": "all",
                "finder": f'ById;Id="{job.job_id}",siteNumber={site}',
            }
            response = await self.http.request("GET", api, params=params)
            detail_url = str(response.url)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError(f"Aster CX detail for exact ID {job.job_id} was not JSON") from exc
            rows = payload.get("items") or []
            if len(rows) != 1 or str(rows[0].get("Id")) != str(job.job_id):
                raise ValueError("Aster CX detail did not return the exact published posting")
            row = rows[0]
            if row.get("Title") != job.title:
                raise ValueError("Aster CX detail title does not match the exact listing ID")

            job.raw["detail"] = row
            job.raw["detail_response_evidence"] = {
                "url": detail_url,
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "fetched_at": now(),
            }
            job.title = row.get("Title") or job.title
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else None
            job.date_posted = row.get("ExternalPostedStartDate") or job.date_posted
            job.date_posted_raw = (job.raw.get("current_listing") or {}).get("PostedDate")
            job.valid_through = row.get("ExternalPostedEndDate")
            job.locations = self._location_values(row) or job.locations
            job.department = row.get("Department") or row.get("Category") or job.department
            job.team = row.get("JobFunction") or row.get("JobFamily")
            job.employment_type = row.get("JobSchedule") or row.get("JobType") or job.employment_type
            job.workplace_type = row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or job.workplace_type
            job.qualifications = row.get("ExternalQualificationsStr") or None
            job.responsibilities = row.get("ExternalResponsibilitiesStr") or None
            job.education = row.get("StudyLevel")
            job.language = row.get("ContentLocale") or job.language
            job.employer = row.get("LegalEmployer")
            job.experience = {
                key: row.get(key) for key in ("WorkYears", "WorkMonths")
                if row.get(key) is not None
            } or None
            job.description_source_url = detail_url
            job.description_method = "oracle.external_full_fields"
            job.fetched_at = now()

            role_sections = [
                str(row.get(field) or "") for field in self.ROLE_FIELDS
            ]
            has_role_body = any(text_html(value) for value in role_sections)
            if not has_role_body:
                # Aster's native detail route can carry an Open Graph preview or
                # a map attribution in description-themed markup. Neither is a
                # full role section; inspect the exact same job route and retain
                # its metadata without promoting it to the body.
                page = await self.http.get(job.url)
                soup = BeautifulSoup(page.text, "html.parser")
                meta = soup.find("meta", attrs={"property": "og:description"})
                preview = meta.get("content", "") if meta else ""
                title_meta = soup.find("meta", attrs={"property": "og:title"})
                job.raw["job_page_evidence"] = {
                    "url": str(page.url),
                    "sha256": hashlib.sha256(page.content).hexdigest(),
                    "fetched_at": now(),
                    "og_title": title_meta.get("content", "") if title_meta else None,
                    "og_description_preview": preview,
                    "og_description_chars": len(preview),
                    "og_description_truncated": bool(re.search(r"(?:\.\.\.|…)$", preview.strip())),
                    "rendered_role_body_used": False,
                }
                job.description_html = ""
                job.description_text = ""
                job.description_sha256 = None
                job.description_verified = False
                job.description_unavailable = True
                for warning in ("source_description_missing", "description_not_full_field"):
                    if warning not in job.warnings:
                        job.warnings.append(warning)
                return job

            job.description_html = self._body_from_detail(row)
            job.description_text = text_html(job.description_html)
            job.description_unavailable = False
            job.description_verified = False
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        self.result.metadata["archived_detail_reuse"] = {
            **self._archive_stats,
            "detail_phase": self.result.metadata.get("details", {}),
        }
        unavailable_ids = sorted(
            (job.job_id for job in self.result.jobs if job.description_unavailable),
            key=lambda value: (0, int(value)) if value and str(value).isdigit() else (1, str(value)),
        )
        self.result.metadata["source_empty_role_ids"] = unavailable_ids
        self.result.metadata["source_empty_role_count"] = len(unavailable_ids)
        return self.result
