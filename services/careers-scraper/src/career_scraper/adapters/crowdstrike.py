"""CrowdStrike's official careers page and linked global Workday careers board.

The CrowdStrike careers page publishes a Search Jobs action to the exact
`crowdstrikecareers` Workday site
(`https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers`).
This adapter starts from the CrowdStrike-owned entry, verifies that site
identity, then reuses the Workday CXS job interface only for unfiltered
postings from that company-published site.
"""

from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..models import text_html
from .workday import Workday


OFFICIAL_ENTRY = "https://www.crowdstrike.com/en-us/careers/"
PUBLISHED_SEARCH_URL = "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"
PUBLISHED_HOST = "crowdstrike.wd5.myworkdayjobs.com"
TENANT = "crowdstrike"
SITE = "crowdstrikecareers"
PAGE_SIZE = 20
VERIFICATION_REVISION = "crowdstrike.official-careers-crowdstrikecareers-global-listing.1"


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
            or parsed.path != "/crowdstrikecareers"
            or canonical_url != PUBLISHED_SEARCH_URL
            or identity["tenant"] != TENANT or identity["site_id"] != SITE):
        raise ValueError("CrowdStrike's official Search Jobs destination no longer identifies its published Workday site")
    return identity


def _entry_identity(html: str, final_url: str) -> dict[str, str]:
    """Confirm the source response is CrowdStrike's official career entry."""
    parsed = urlsplit(final_url)
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.select_one('meta[name="description"]')
    description = meta.get("content", "") if meta else ""
    hrefs = sorted({a.get("href", "") for a in soup.select("a[href]")
                    if PUBLISHED_HOST in a.get("href", "")})
    identity = {"host": parsed.hostname or "", "path": parsed.path,
                "title": title, "description": description[:200],
                "published_board_hrefs": hrefs}
    if (parsed.scheme != "https" or parsed.hostname not in {"www.crowdstrike.com", "crowdstrike.com"}
            or parsed.path != "/en-us/careers/" or title != "Careers | CrowdStrike"
            or not any(href.startswith(PUBLISHED_SEARCH_URL) for href in hrefs)):
        raise ValueError("CrowdStrike's official careers entry response changed or no longer publishes its Workday board")
    return identity


class Crowdstrike(Workday):
    """Fetch the exact public postings reached from CrowdStrike's Search Jobs UI."""

    async def _final_membership(self, api_base: str, expected_total: int) -> dict:
        started = time.perf_counter()
        offset = 0
        page_offsets: list[int] = []
        paths: list[str] = []
        first_total: int | None = None
        page_errors: list[str] = []

        while len(paths) < expected_total:
            if len(page_offsets) >= self.options.max_pages:
                page_errors.append("crowdstrike_final_page_budget_exhausted")
                break
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": ""}
            data = await self.http.json(api_base + "/jobs", method="POST", json=payload)
            rows = data.get("jobPostings")
            if not isinstance(rows, list):
                page_errors.append(f"crowdstrike_final_listing_missing_rows_at_offset_{offset}")
                break
            total = data.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                page_errors.append(f"crowdstrike_final_listing_invalid_total_at_offset_{offset}")
                break
            if first_total is None:
                first_total = total
            elif total not in (0, first_total):
                page_errors.append(f"crowdstrike_final_total_changed:{first_total}->{total}")
            if first_total != expected_total:
                page_errors.append(f"crowdstrike_final_total_changed:{expected_total}->{first_total}")
            if len(rows) > PAGE_SIZE:
                page_errors.append(f"crowdstrike_final_page_over_limit_at_offset_{offset}:{len(rows)}")
                break
            if not rows:
                page_errors.append(f"crowdstrike_final_listing_empty_page_at_offset_{offset}")
                break

            page_offsets.append(offset)
            for row in rows:
                path = row.get("externalPath") if isinstance(row, dict) else None
                if not isinstance(path, str) or not path.startswith("/job/"):
                    page_errors.append(f"crowdstrike_final_listing_bad_external_path_at_offset_{offset}")
                    continue
                paths.append(path)
            offset += len(rows)
            if offset > expected_total + PAGE_SIZE:
                page_errors.append("crowdstrike_final_listing_exceeded_expected_membership")
                break

        unique_paths = set(paths)
        if len(unique_paths) != len(paths):
            page_errors.append("crowdstrike_final_listing_duplicate_external_paths")
        if len(unique_paths) != expected_total:
            page_errors.append(f"crowdstrike_final_listing_membership_count:{len(unique_paths)}/{expected_total}")
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
                self.result.errors.append(f"crowdstrike_missing_detail_payload:{job.url}")
                job.warnings.append("crowdstrike_exact_detail_payload_unverified")
                job.description_unavailable = True
                job.description_verified = False
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
                "detail_description_present": bool(str(info.get("jobDescription") or "").strip()),
            }
            listing["detail_identity_audit"] = checks
            if not all(checks.values()):
                for check, passed in checks.items():
                    if passed:
                        continue
                    self.result.errors.append(f"crowdstrike_{check}_failed:{req_id or external_path or job.url}")
                    job.warnings.append("crowdstrike_exact_detail_identity_unverified")
                    job.description_unavailable = True
                    job.description_verified = False

            if isinstance(external_path, str) and external_path.startswith("/job/"):
                job.url = f"https://{PUBLISHED_HOST}/{SITE}{external_path}"
            if isinstance(title, str) and title:
                job.title = title
            job.job_id = req_id if isinstance(req_id, str) else job.job_id
            job.requisition_id = job.job_id
            job.apply_url = job.url
            job.date_posted = info.get("startDate") or None
            job.date_posted_raw = {"postedOn": info.get("postedOn"), "startDate": info.get("startDate")}
            job.valid_through = info.get("endDate") or None
            job.employment_type = info.get("timeType") or listing.get("timeType")
            location = info.get("jobRequisitionLocation") or info.get("location")
            job.locations = ([location] if location else []) + (info.get("additionalLocations") or [])
            job.description_html = info.get("jobDescription") or ""
            job.description_text = text_html(job.description_html)
            job.validate()

    async def run(self):
        if self.company.careers_url != OFFICIAL_ENTRY or self.source.url != OFFICIAL_ENTRY:
            raise ValueError("CrowdStrike adapter must start at its official careers entry")

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
            "listing_authority": "CrowdStrike official Search Jobs journey to its global crowdstrikecareers Workday site",
            "workday_site_identity": site_identity,
            "request_source": "Published Workday CXS unfiltered jobs listing and exact listed-job detail requests",
            "native_page_size": PAGE_SIZE,
            "independent_rendered_reference": "companies/crowdstrike/browser-reference-parent.json (if present)",
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
        # Record role-bearing attachment links found in descriptions; no extra
        # fetch is performed for links that are not posting documents.
        attachment_links = {}
        for job in self.result.jobs:
            soup = BeautifulSoup(job.description_html or "", "lxml")
            links = []
            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "").strip()
                if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
                    links.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
            if links:
                attachment_links[job.job_id] = links
        self.result.metadata["job_attachment_links"] = attachment_links

        initial_paths = sorted({
            job.raw.get("externalPath") for job in self.result.jobs
            if isinstance(job.raw, dict) and isinstance(job.raw.get("externalPath"), str)
        })
        initial_total = self.result.expected_total
        if initial_total is None:
            self.result.errors.append("crowdstrike_initial_listing_has_no_total")
            self.result.listing_complete = False
        elif initial_total == 0:
            self.result.errors.append("crowdstrike_empty_listing_requires_current_rendered_confirmation")
            self.result.listing_complete = False
        else:
            recheck = await self._final_membership(api_base, initial_total)
            self.result.timings["final_membership_seconds"] = recheck.pop("duration_seconds")
            recheck["initial_unique_count"] = len(initial_paths)
            recheck["initial_external_paths"] = initial_paths
            recheck["initial_external_paths_sha256"] = sha256("\n".join(initial_paths).encode()).hexdigest()
            recheck["same_id_set_as_initial"] = set(initial_paths) == set(recheck["external_paths"])
            if not recheck["same_id_set_as_initial"]:
                recheck["errors"].append("crowdstrike_initial_final_membership_changed")
            if len(initial_paths) != initial_total or len(self.result.jobs) != initial_total:
                recheck["errors"].append(
                    f"crowdstrike_initial_listing_membership_count:{len(initial_paths)}/{len(self.result.jobs)}/{initial_total}")
            if recheck["errors"]:
                self.result.errors.extend(recheck["errors"])
                self.result.listing_complete = False
            self.result.metadata["membership_reconciliation"] = recheck

        self.result.metadata["initial_page_count"] = self.result.pages
        self.result.metadata["initial_expected_total"] = self.result.expected_total
        return self.result
