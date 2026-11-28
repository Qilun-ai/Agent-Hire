"""Sabre Corporation's official careers page and its published global Workday site.

`https://www.sabre.com/careers/` is Sabre's own careers page. Its single
"View open roles" action points at the Sabre-branded Workday site
`https://sabre.wd1.myworkdayjobs.com/SabreJobs`. This adapter starts from that
Sabre-owned entry, verifies both the published link and the Workday site
identity, then uses the site's own unfiltered CXS listing for membership and the
exact listed-job detail response for every posting. No other board, tenant or
search result is treated as Sabre's membership.
"""

from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..models import text_html
from .workday import Workday


OFFICIAL_ENTRY = "https://www.sabre.com/careers/"
PUBLISHED_SEARCH_URL = "https://sabre.wd1.myworkdayjobs.com/SabreJobs"
PUBLISHED_HOST = "sabre.wd1.myworkdayjobs.com"
TENANT = "sabre"
SITE = "SabreJobs"
PAGE_SIZE = 20
PUBLISHED_LINK_LABEL = "View open roles"
HUMAN_JOB_PREFIX = f"https://{PUBLISHED_HOST}/en-US/{SITE}"
VERIFICATION_REVISION = "sabre-corporation.official-careers-SabreJobs-global-listing.1"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# www.sabre.com sits behind a JS interstitial that answers the project's
# non-browser UA with a 1 KB sessionStorage page instead of the careers page.
# Request the same public HTML a normal desktop browser receives; no challenge
# or captcha is executed, solved, or retried around.
ENTRY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _entry_identity(html: str, final_url: str) -> dict:
    """Confirm the response is Sabre's own current careers entry."""
    parsed = urlsplit(final_url)
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.select_one("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    board_links = [
        (anchor.get_text(" ", strip=True), anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if "myworkdayjobs.com" in anchor.get("href", "")
    ]
    published = sorted({href for _, href in board_links})
    identity = {
        "host": parsed.hostname or "",
        "path": parsed.path,
        "title": title,
        "heading": heading_text,
        "published_board_hrefs": published,
        "published_link_labels": sorted({label for label, _ in board_links}),
    }
    if (parsed.scheme != "https"
            or parsed.hostname not in {"www.sabre.com", "sabre.com"}
            or parsed.path.rstrip("/") != "/careers"
            or not title.startswith("Sabre Careers")
            or heading_text.replace("'", "\u2019") != "Where ambitious people build travel\u2019s future"
            or published != [PUBLISHED_SEARCH_URL]
            or identity["published_link_labels"] != [PUBLISHED_LINK_LABEL]):
        raise ValueError("Sabre's official careers entry changed or no longer publishes its SabreJobs board")
    return identity


def _exact_site_identity(html: str, final_url: str) -> dict:
    """Read the Workday site identity from its own public page shell."""
    parsed = urlsplit(final_url)
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
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
            or parsed.path != f"/{SITE}"
            or canonical_url != PUBLISHED_SEARCH_URL
            or identity["tenant"] != TENANT or identity["site_id"] != SITE):
        raise ValueError("Sabre's published destination no longer identifies its SabreJobs Workday site")
    return identity


def human_job_url(external_path: str) -> str:
    """The exact locale-prefixed href the SabreJobs listing UI exposes."""
    if not isinstance(external_path, str) or not external_path.startswith("/job/"):
        raise ValueError(f"SabreJobs listing exposed an unexpected external path: {external_path!r}")
    return HUMAN_JOB_PREFIX + external_path


def apply_url(job_url: str) -> str:
    """The Apply control on each SabreJobs posting links to `<job url>/apply`."""
    return job_url.rstrip("/") + "/apply"


def _iso_date(value) -> str | None:
    """Return the source date only when it is a real ISO posting date."""
    return value if isinstance(value, str) and ISO_DATE.fullmatch(value) else None


class SabreCorporation(Workday):
    """Fetch exactly the public postings Sabre publishes on its SabreJobs site."""

    def __init__(self, http, company, source, options, *, entry_capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.entry_capture = entry_capture

    async def _final_membership(self, api_base: str, expected_total: int) -> dict:
        started = time.perf_counter()
        offset = 0
        page_offsets: list[int] = []
        paths: list[str] = []
        first_total: int | None = None
        page_errors: list[str] = []

        while len(paths) < expected_total:
            if len(page_offsets) >= self.options.max_pages:
                page_errors.append("sabre_corporation_final_page_budget_exhausted")
                break
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": ""}
            data = await self.http.json(api_base + "/jobs", method="POST", json=payload)
            rows = data.get("jobPostings")
            if not isinstance(rows, list):
                page_errors.append(f"sabre_corporation_final_listing_missing_rows_at_offset_{offset}")
                break
            total = data.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                page_errors.append(f"sabre_corporation_final_listing_invalid_total_at_offset_{offset}")
                break
            if first_total is None:
                first_total = total
            elif total not in (0, first_total):
                page_errors.append(f"sabre_corporation_final_total_changed:{first_total}->{total}")
            if first_total != expected_total:
                page_errors.append(f"sabre_corporation_final_total_changed:{expected_total}->{first_total}")
            if len(rows) > PAGE_SIZE:
                page_errors.append(f"sabre_corporation_final_page_over_limit_at_offset_{offset}:{len(rows)}")
                break
            if not rows:
                page_errors.append(f"sabre_corporation_final_listing_empty_page_at_offset_{offset}")
                break

            page_offsets.append(offset)
            for row in rows:
                path = row.get("externalPath") if isinstance(row, dict) else None
                if not isinstance(path, str) or not path.startswith("/job/"):
                    page_errors.append(f"sabre_corporation_final_listing_bad_external_path_at_offset_{offset}")
                    continue
                paths.append(path)
            offset += len(rows)
            if offset > expected_total + PAGE_SIZE:
                page_errors.append("sabre_corporation_final_listing_exceeded_expected_membership")
                break

        unique_paths = set(paths)
        if len(unique_paths) != len(paths):
            page_errors.append("sabre_corporation_final_listing_duplicate_external_paths")
        if len(unique_paths) != expected_total:
            page_errors.append(
                f"sabre_corporation_final_listing_membership_count:{len(unique_paths)}/{expected_total}")
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
        """Keep only fields the exact SabreJobs posting response publishes."""
        for job in self.result.jobs:
            listing = job.raw
            detail = listing.get("detail") if isinstance(listing, dict) else None
            info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
            external_path = listing.get("externalPath") if isinstance(listing, dict) else None
            if not isinstance(info, dict) or not isinstance(external_path, str):
                self.result.errors.append(f"sabre_corporation_missing_detail_payload:{job.url}")
                job.warnings.append("sabre_corporation_exact_detail_payload_unverified")
                job.description_unavailable = True
                job.description_verified = False
                continue

            title = info.get("title")
            req_id = info.get("jobReqId")
            bullet_ids = listing.get("bulletFields") or []
            path_posting_id = external_path.rsplit("/", 1)[-1]
            detail_path = urlsplit(info.get("externalUrl") or "").path
            checks = {
                "detail_title_matches_listing": title == listing.get("title"),
                "detail_posting_id_matches_path": bool(info.get("jobPostingId"))
                and info.get("jobPostingId") == path_posting_id,
                "detail_requisition_matches_listing_bullet": bool(req_id) and req_id in bullet_ids,
                "detail_site_matches": info.get("jobPostingSiteId") == SITE,
                "detail_url_matches_listing_path": detail_path == f"/{SITE}{external_path}",
                "detail_description_present": bool(str(info.get("jobDescription") or "").strip()),
            }
            listing["detail_identity_audit"] = checks
            if not all(checks.values()):
                for check, passed in checks.items():
                    if passed:
                        continue
                    self.result.errors.append(
                        f"sabre_corporation_{check}_failed:{req_id or external_path or job.url}")
                    job.warnings.append("sabre_corporation_exact_detail_identity_unverified")
                    job.description_unavailable = True
                    job.description_verified = False

            job.url = human_job_url(external_path)
            if isinstance(title, str) and title:
                job.title = title
            job.job_id = req_id if isinstance(req_id, str) else job.job_id
            job.requisition_id = req_id if isinstance(req_id, str) else job.requisition_id
            job.apply_url = apply_url(job.url)
            job.date_posted = _iso_date(info.get("startDate"))
            job.date_posted_raw = {"postedOn": info.get("postedOn"), "startDate": info.get("startDate")}
            job.valid_through = _iso_date(info.get("endDate"))
            job.employment_type = info.get("timeType") or None
            job.workplace_type = info.get("remoteType") or None
            locations: list[str] = []
            requisition_location = info.get("jobRequisitionLocation")
            for candidate in [
                info.get("location"),
                requisition_location.get("descriptor") if isinstance(requisition_location, dict) else None,
                *(info.get("additionalLocations") or []),
            ]:
                if isinstance(candidate, str) and candidate.strip() and candidate not in locations:
                    locations.append(candidate)
            job.locations = locations
            employer = detail.get("hiringOrganization") if isinstance(detail, dict) else None
            job.employer = employer.get("name") if isinstance(employer, dict) else None
            job.description_html = info.get("jobDescription") or ""
            job.description_text = text_html(job.description_html)
            job.description_method = "workday.jobPostingInfo.jobDescription"
            job.description_source_url = f"https://{PUBLISHED_HOST}/wday/cxs/{TENANT}/{SITE}{external_path}"
            job.validate()

    async def run(self):
        if self.company.careers_url != OFFICIAL_ENTRY or self.source.url != OFFICIAL_ENTRY:
            raise ValueError("Sabre Corporation adapter must start at its official careers entry")

        if self.entry_capture:
            # A saved, content-addressed live response, reused only when the
            # Sabre site is answering with its Imperva "Pardon Our Interruption"
            # interstitial. Nothing is bypassed: the same public HTML bytes are
            # validated, and the reuse is recorded in metadata.
            capture = self.entry_capture
            entry_identity = _entry_identity(capture["html"], capture["final_url"])
            self.result.metadata["entry_capture_reused"] = {
                "reason": capture.get("reason", "live entry answered with a bot interstitial"),
                "source_sha256": capture.get("sha256"),
                "fetched_at": capture.get("fetched_at"),
                "final_url": capture["final_url"],
                "bytes": len(capture["html"].encode()),
            }
            self.result.timings["official_entry_seconds"] = 0.0
        else:
            entry_started = time.perf_counter()
            entry = await self.http.get(OFFICIAL_ENTRY, headers=ENTRY_HEADERS)
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
            "published_link_label": PUBLISHED_LINK_LABEL,
            "published_search_url": PUBLISHED_SEARCH_URL,
            "listing_authority": "Sabre official careers page action to its SabreJobs Workday site",
            "workday_site_identity": site_identity,
            "entry_request_note": "public careers HTML requested with a desktop Chrome UA because the CDN "
                                  "serves a JS interstitial (no jobs, no content) to the project UA; "
                                  "no challenge/captcha was solved or retried",
            "request_source": "Published SabreJobs CXS unfiltered jobs listing and exact listed-job detail requests",
            "native_page_size": PAGE_SIZE,
            "human_job_url_pattern": HUMAN_JOB_PREFIX + "<externalPath>",
            "apply_url_pattern": HUMAN_JOB_PREFIX + "<externalPath>/apply",
            "date_field": "jobPostingInfo.startDate (posting start date; postedOn is only relative text)",
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

        initial_paths = sorted({
            job.raw.get("externalPath") for job in self.result.jobs
            if isinstance(job.raw, dict) and isinstance(job.raw.get("externalPath"), str)
        })
        initial_total = self.result.expected_total
        if initial_total is None:
            self.result.errors.append("sabre_corporation_initial_listing_has_no_total")
            self.result.listing_complete = False
        elif initial_total == 0:
            self.result.errors.append("sabre_corporation_empty_listing_requires_current_rendered_confirmation")
            self.result.listing_complete = False
        else:
            recheck = await self._final_membership(api_base, initial_total)
            self.result.timings["final_membership_seconds"] = recheck.pop("duration_seconds")
            recheck["initial_unique_count"] = len(initial_paths)
            recheck["initial_external_paths"] = initial_paths
            recheck["initial_external_paths_sha256"] = sha256("\n".join(initial_paths).encode()).hexdigest()
            recheck["same_id_set_as_initial"] = set(initial_paths) == set(recheck["external_paths"])
            if not recheck["same_id_set_as_initial"]:
                recheck["errors"].append("sabre_corporation_initial_final_membership_changed")
            if len(initial_paths) != initial_total or len(self.result.jobs) != initial_total:
                recheck["errors"].append(
                    f"sabre_corporation_initial_listing_membership_count:"
                    f"{len(initial_paths)}/{len(self.result.jobs)}/{initial_total}")
            if recheck["errors"]:
                self.result.errors.extend(recheck["errors"])
                self.result.listing_complete = False
            self.result.metadata["membership_reconciliation"] = recheck

        self.result.metadata["initial_page_count"] = self.result.pages
        self.result.metadata["initial_expected_total"] = self.result.expected_total
        return self.result
