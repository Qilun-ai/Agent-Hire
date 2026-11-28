"""Broadcom's official careers page and linked global Workday careers board.

The Broadcom careers page publishes a Search Jobs action to the exact
`External_Career` Workday site. This adapter starts from the Broadcom-owned
entry, verifies that site identity, then reuses the Workday CXS job interface
only for unfiltered postings from that company-published site.
"""

from __future__ import annotations

from html import escape
from hashlib import sha256
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..attachments import extract_attachment
from ..models import text_html
from .workday import Workday


OFFICIAL_ENTRY = "https://www.broadcom.com/company/careers"
PUBLISHED_SEARCH_URL = "https://broadcom.wd1.myworkdayjobs.com/External_Career"
PUBLISHED_HOST = "broadcom.wd1.myworkdayjobs.com"
TENANT = "broadcom"
SITE = "External_Career"
PAGE_SIZE = 20
VERIFICATION_REVISION = "broadcom.official-careers-External_Career-global-listing.1"


def _exact_site_identity(html: str, final_url: str) -> dict[str, str]:
    """Read the Workday site identity from its own public page shell."""
    parsed = urlsplit(final_url)
    canonical = BeautifulSoup(html, "lxml").select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    tenant = re.search(r'\btenant\s*:\s*["\']([^"\']+)["\']', html)
    site = re.search(r'\bsiteId\s*:\s*["\']([^"\']+)["\']', html)
    request_locale = re.search(r'\brequestLocale\s*:\s*["\']([^"\']+)["\']', html)
    identity = {
        "host": parsed.hostname or "",
        "path": parsed.path,
        "canonical_url": canonical_url,
        "tenant": tenant.group(1) if tenant else "",
        "site_id": site.group(1) if site else "",
        "request_locale": request_locale.group(1) if request_locale else "",
    }
    if (parsed.scheme != "https" or parsed.hostname != PUBLISHED_HOST
            or parsed.path != "/External_Career"
            or canonical_url != PUBLISHED_SEARCH_URL
            or identity["tenant"] != TENANT or identity["site_id"] != SITE):
        raise ValueError("Broadcom's official Search Jobs destination no longer identifies its published Workday site")
    return identity


def _entry_identity(html: str, final_url: str) -> dict[str, str]:
    """Confirm the source response is Broadcom's official career entry."""
    parsed = urlsplit(final_url)
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.select_one('meta[name="description"]')
    description = meta.get("content", "") if meta else ""
    identity = {"host": parsed.hostname or "", "path": parsed.path,
                "title": title, "description": description}
    if (parsed.scheme != "https" or parsed.hostname not in {"www.broadcom.com", "broadcom.com"}
            or parsed.path != "/company/careers" or title != "Careers | Broadcom"
            or "Broadcom is hiring" not in description):
        raise ValueError("Broadcom's official careers entry response changed or is not identifiable")
    return identity


class Broadcom(Workday):
    """Fetch the exact public postings reached from Broadcom's Search Jobs UI."""

    async def _final_membership(self, api_base: str, expected_total: int) -> dict:
        started = time.perf_counter()
        offset = 0
        page_offsets: list[int] = []
        paths: list[str] = []
        first_total: int | None = None
        page_errors: list[str] = []

        while len(paths) < expected_total:
            if len(page_offsets) >= self.options.max_pages:
                page_errors.append("broadcom_final_page_budget_exhausted")
                break
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": ""}
            data = await self.http.json(api_base + "/jobs", method="POST", json=payload)
            rows = data.get("jobPostings")
            if not isinstance(rows, list):
                page_errors.append(f"broadcom_final_listing_missing_rows_at_offset_{offset}")
                break
            total = data.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                page_errors.append(f"broadcom_final_listing_invalid_total_at_offset_{offset}")
                break
            if first_total is None:
                first_total = total
            elif total not in (0, first_total):
                page_errors.append(f"broadcom_final_total_changed:{first_total}->{total}")
            if first_total != expected_total:
                page_errors.append(f"broadcom_final_total_changed:{expected_total}->{first_total}")
            if len(rows) > PAGE_SIZE:
                page_errors.append(f"broadcom_final_page_over_limit_at_offset_{offset}:{len(rows)}")
                break
            if not rows:
                page_errors.append(f"broadcom_final_listing_empty_page_at_offset_{offset}")
                break

            page_offsets.append(offset)
            for row in rows:
                path = row.get("externalPath") if isinstance(row, dict) else None
                if not isinstance(path, str) or not path.startswith("/job/"):
                    page_errors.append(f"broadcom_final_listing_bad_external_path_at_offset_{offset}")
                    continue
                paths.append(path)
            offset += len(rows)
            if offset > expected_total + PAGE_SIZE:
                page_errors.append("broadcom_final_listing_exceeded_expected_membership")
                break

        unique_paths = set(paths)
        if len(unique_paths) != len(paths):
            page_errors.append("broadcom_final_listing_duplicate_external_paths")
        if len(unique_paths) != expected_total:
            page_errors.append(f"broadcom_final_listing_membership_count:{len(unique_paths)}/{expected_total}")
        return {
            "total": first_total,
            "page_size": PAGE_SIZE,
            "page_offsets": page_offsets,
            "page_count": len(page_offsets),
            "returned_rows": len(paths),
            "unique_count": len(unique_paths),
            "external_paths": sorted(unique_paths),
            "external_paths_sha256": sha256("\n".join(sorted(unique_paths)).encode()).hexdigest(),
            "errors": page_errors,
            "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    def _audit_exact_details(self) -> None:
        for job in self.result.jobs:
            listing = job.raw
            detail = listing.get("detail") if isinstance(listing, dict) else None
            info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
            external_path = listing.get("externalPath") if isinstance(listing, dict) else None
            if not isinstance(info, dict):
                continue

            title = info.get("title")
            req_id = info.get("jobReqId")
            bullet_ids = listing.get("bulletFields") or []
            path_posting_id = external_path.rsplit("/", 1)[-1] if isinstance(external_path, str) else ""
            checks = {
                "detail_title_matches_listing": title == listing.get("title"),
                "detail_posting_id_matches_path": bool(info.get("jobPostingId")) and info.get("jobPostingId") == path_posting_id,
                "detail_requisition_matches_listing_bullet": not bullet_ids or req_id in bullet_ids,
                "detail_site_matches": info.get("jobPostingSiteId") == SITE,
                "detail_description_present": bool(info.get("jobDescription")),
            }
            listing["detail_identity_audit"] = checks
            if not all(checks.values()):
                for check, passed in checks.items():
                    if passed:
                        continue
                    self.result.errors.append(f"broadcom_{check}_failed:{req_id or external_path or job.url}")
                    job.warnings.append("broadcom_exact_detail_identity_unverified")
                    job.description_unavailable = True
                    job.description_verified = False
            elif "broadcom_role_attachment_unverified" not in job.warnings:
                job.warnings = [warning for warning in job.warnings
                                if warning != "broadcom_exact_detail_identity_unverified"]
                job.description_unavailable = False

            if isinstance(external_path, str) and external_path.startswith("/job/"):
                job.url = f"https://{PUBLISHED_HOST}/en-US/{SITE}{external_path}"
            if isinstance(title, str) and title:
                job.title = title
            job.job_id = req_id if isinstance(req_id, str) else job.job_id
            job.requisition_id = job.job_id
            job.apply_url = job.url + "/apply"
            job.date_posted = info.get("startDate") or None
            job.date_posted_raw = {"postedOn": info.get("postedOn"), "startDate": info.get("startDate")}
            job.valid_through = info.get("endDate") or None
            job.employment_type = info.get("timeType") or listing.get("timeType")
            location = info.get("jobRequisitionLocation") or info.get("location")
            job.locations = ([location] if location else []) + (info.get("additionalLocations") or [])
            employer = detail.get("hiringOrganization") if isinstance(detail, dict) else None
            if isinstance(employer, dict):
                job.employer = employer.get("name") or employer
            elif employer:
                job.employer = employer
            job.validate()

    async def _fetch_role_attachments(self) -> None:
        supported = {".pdf", ".doc", ".docx"}
        for job in self.result.jobs:
            detail = job.raw.get("detail", {}) if isinstance(job.raw, dict) else {}
            info = detail.get("jobPostingInfo", {}) if isinstance(detail, dict) else {}
            description = info.get("jobDescription", "") if isinstance(info, dict) else ""
            attachments = []
            seen_urls = set()
            soup = BeautifulSoup(description or "", "lxml")
            for anchor in soup.select("a[href]"):
                url = urljoin(job.url, anchor.get("href", ""))
                suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
                if suffix not in supported or url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    response = await self.http.get(url)
                    extracted = extract_attachment(response.content, PurePosixPath(urlsplit(url).path).name)
                    attachment = {
                        "url": url,
                        "label": anchor.get_text(" ", strip=True),
                        "source_field": "jobPostingInfo.jobDescription",
                        "content_type": response.headers.get("content-type"),
                        **extracted,
                    }
                    attachments.append(attachment)
                    job.description_html += "\n<pre>" + escape(extracted.get("text", "")) + "</pre>"
                except Exception as exc:
                    identity = job.requisition_id or job.job_id or job.url
                    self.result.errors.append(f"broadcom_attachment_fetch_failed:{identity}:{url}:{type(exc).__name__}:{exc}")
                    job.warnings.append("broadcom_role_attachment_unverified")
                    job.description_unavailable = True
                    job.description_verified = False
            job.raw["attachments"] = attachments
            if attachments:
                job.description_text = text_html(job.description_html)

    async def run(self):
        if self.company.careers_url != OFFICIAL_ENTRY or self.source.url != OFFICIAL_ENTRY:
            raise ValueError("Broadcom adapter must start at its official careers entry")

        entry_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_identity = _entry_identity(entry.text, str(entry.url))
        self.result.timings["official_entry_seconds"] = round(
            max(0.0, time.perf_counter() - entry_started), 6)

        shell_started = time.perf_counter()
        board = await self.http.get(
            PUBLISHED_SEARCH_URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        site_identity = _exact_site_identity(board.text, str(board.url))
        self.result.timings["workday_shell_seconds"] = round(
            max(0.0, time.perf_counter() - shell_started), 6)
        self.result.metadata.update({
            "verification_revision": VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_identity": entry_identity,
            "published_link_label": "Search Jobs",
            "published_search_url": PUBLISHED_SEARCH_URL,
            "listing_authority": "Broadcom official Search Jobs journey to its global External_Career Workday site",
            "workday_site_identity": site_identity,
            "request_source": "Published Workday CXS unfiltered jobs listing and exact listed-job detail requests",
            "native_page_size": PAGE_SIZE,
            "independent_rendered_reference": "companies/broadcom/browser-reference-parent.json",
        })

        api_base = f"https://{PUBLISHED_HOST}/wday/cxs/{TENANT}/{SITE}"
        original_url = self.source.url
        self.source.url = PUBLISHED_SEARCH_URL
        listing_started = time.perf_counter()
        try:
            await super().run()
        finally:
            self.source.url = original_url
        collection_seconds = max(0.0, time.perf_counter() - listing_started)
        detail_seconds = self.result.timings.get("detail_phase_seconds", 0.0)
        self.result.timings["initial_listing_seconds"] = round(
            max(0.0, collection_seconds - detail_seconds), 6)
        self.result.timings["workday_listing_and_detail_seconds"] = round(collection_seconds, 6)

        self._audit_exact_details()
        await self._fetch_role_attachments()
        initial_paths = sorted({
            job.raw.get("externalPath") for job in self.result.jobs
            if isinstance(job.raw, dict) and isinstance(job.raw.get("externalPath"), str)
        })
        initial_total = self.result.expected_total
        if initial_total is None:
            self.result.errors.append("broadcom_initial_listing_has_no_total")
            self.result.listing_complete = False
        elif initial_total == 0:
            # A zero from this endpoint cannot silently certify an empty board;
            # the current independent rendered reference reports live postings.
            self.result.errors.append("broadcom_empty_listing_requires_current_rendered_confirmation")
            self.result.listing_complete = False
        else:
            recheck = await self._final_membership(api_base, initial_total)
            self.result.timings["final_membership_seconds"] = recheck.pop("duration_seconds")
            recheck["initial_unique_count"] = len(initial_paths)
            recheck["initial_external_paths"] = initial_paths
            recheck["initial_external_paths_sha256"] = sha256("\n".join(initial_paths).encode()).hexdigest()
            recheck["same_id_set_as_initial"] = set(initial_paths) == set(recheck["external_paths"])
            if not recheck["same_id_set_as_initial"]:
                recheck["errors"].append("broadcom_initial_final_membership_changed")
            if len(initial_paths) != initial_total or len(self.result.jobs) != initial_total:
                recheck["errors"].append(
                    f"broadcom_initial_listing_membership_count:{len(initial_paths)}/{len(self.result.jobs)}/{initial_total}")
            if recheck["errors"]:
                self.result.errors.extend(recheck["errors"])
                self.result.listing_complete = False
            self.result.metadata["membership_reconciliation"] = recheck

        self.result.metadata["initial_page_count"] = self.result.pages
        self.result.metadata["initial_expected_total"] = self.result.expected_total
        return self.result
