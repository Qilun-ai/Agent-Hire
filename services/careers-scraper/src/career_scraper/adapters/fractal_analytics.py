"""Fractal Analytics' own hosted Workday careers journey (tenant fractal, site Careers).

Canonical dedicated adapter module for the company review. The earlier
``adapters/fractal.py`` remains as the original probe module; this module is
the self-contained implementation used by the review runner, auditor and
parent integration.

The official entry ``https://fractal.ai/careers/`` serves a 200 JSON
redirect widget (``{"widget":"redirect","url":"/Careers","externalSpa":true}``)
whose final URL is the Workday site ``https://fractal.wd1.myworkdayjobs.com/Careers``.
Membership is the unfiltered CXS listing
``POST /wday/cxs/fractal/Careers/jobs`` at its site-enforced page size of 20,
followed by one detail ``GET`` per exact listed ``externalPath``. No ATS board
is guessed or enumerated: the adapter refuses any tenant/site other than the
inspected ``fractal``/``Careers`` pair and only ever requests the exact
offsets/paths the published listing exposes.
"""
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

PAGE_LIMIT = 20
ADAPTER_NAME = "fractal_analytics"
EXPECTED_TENANT = "fractal"
EXPECTED_SITE = "Careers"
WORKDAY_HOST = "fractal.wd1.myworkdayjobs.com"
ORIGIN = f"https://{WORKDAY_HOST}"
CANONICAL = f"{ORIGIN}/Careers"
ENTRY_URL = "https://fractal.ai/careers/"
_LISTING_PATH = re.compile(r"^/job/")
# Salary/compensation-ish keys must never appear in a detail payload; the
# adapter records that they are absent instead of inferring pay.
_SALARY_KEY = re.compile(r"salar|compensa|pay\s*range|payrange", re.I)
_ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def job_url(external_path):
    """Canonical hosted job URL for an exact listed path."""
    if not isinstance(external_path, str) or not _LISTING_PATH.match(external_path):
        raise ValueError(f"Fractal listed externalPath is not a /job/ path: {external_path!r}")
    return CANONICAL + external_path


def detail_api_url(external_path):
    job_url(external_path)
    return f"{ORIGIN}/wday/cxs/{EXPECTED_TENANT}/{EXPECTED_SITE}{external_path}"


def listing_api_url():
    return f"{ORIGIN}/wday/cxs/{EXPECTED_TENANT}/{EXPECTED_SITE}/jobs"


def derive_workday_site(final_url, body_text):
    """Resolve (origin, tenant, site) from the entry response, strictly.

    Prefers the HTTP final URL (the repository client follows the official
    redirect to the Workday site); falls back to the published canonical
    link in an HTML shell. Any other tenant/site/host is rejected so the
    flow can never drift to a guessed board.
    """
    parsed = urlsplit(str(final_url or ""))
    if (parsed.scheme == "https" and parsed.hostname == WORKDAY_HOST
            and parsed.netloc == WORKDAY_HOST
            and parsed.path.rstrip("/") == f"/{EXPECTED_SITE}"):
        return ORIGIN, EXPECTED_TENANT, EXPECTED_SITE
    match = re.search(r'href=["\'](' + re.escape(CANONICAL) + r')', body_text or "")
    if match:
        return ORIGIN, EXPECTED_TENANT, EXPECTED_SITE
    # Workday bootstrap shape (browser HTML shell): tenant/siteId literals.
    tenant = re.search(r"tenant\s*:\s*\"([^\"]+)\"", body_text or "")
    site = re.search(r"siteId\s*:\s*\"([^\"]+)\"", body_text or "")
    if tenant and site and tenant.group(1) == EXPECTED_TENANT and site.group(1) == EXPECTED_SITE:
        return ORIGIN, EXPECTED_TENANT, EXPECTED_SITE
    raise ValueError(
        "Fractal official entry does not resolve to its inspected Workday site "
        f"{CANONICAL}: final_url={final_url!r}"
    )


def job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if _ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def assert_listing_body(body_text):
    """Validate that the entry body is the inspected redirect/canonical shell."""
    text = body_text or ""
    if '"widget":"redirect"' in text or '"widget": "redirect"' in text:
        if '"/Careers"' not in text:
            raise ValueError("Fractal entry redirect does not point at /Careers")
        return
    if CANONICAL in text:
        return
    raise ValueError("Fractal entry body is neither the inspected redirect widget nor the canonical shell")


class FractalAnalytics(Adapter):
    """Collect Fractal postings only through its official Workday site."""

    VERIFICATION_REVISION = "fractal.workday-careers.cxs-official-site.1"

    @staticmethod
    def _apply_listing_row(job, row):
        """Refresh volatile listing metadata without touching detail provenance.

        A reused detail body keeps its original ``raw.detail`` payload, but the
        current public listing row (title/locationsText/postedOn) must be the
        one stored for this snapshot. Locations that fall back to
        ``locationsText`` are recomputed so a moved/reworded card is not
        reported with stale location text.
        """
        job.raw["listing"] = row
        info = (job.raw.get("detail") or {}).get("jobPostingInfo") or {}
        locations = []
        if info.get("location"):
            locations.append(info["location"])
        locations.extend(info.get("additionalLocations") or [])
        if not locations and row.get("locationsText"):
            locations = [row["locationsText"]]
        job.locations = locations
        if not job.title and row.get("title"):
            job.title = row["title"]

    @staticmethod
    def _listing_rows(payload, label):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list):
            raise ValueError(f"Fractal {label} listing response has no jobPostings array")
        rows = payload["jobPostings"]
        for row in rows:
            if not isinstance(row, dict) or not row.get("externalPath") or not row.get("title"):
                raise ValueError(f"Fractal {label} listing contains an unidentified row: {row!r}")
            job_url(row["externalPath"])
        paths = [row["externalPath"] for row in rows]
        if len(paths) != len(set(paths)):
            raise ValueError(f"Fractal {label} listing repeats a company-published path")
        return rows

    async def _listing_snapshot(self, label):
        """Fully paginate the unfiltered CXS listing; record page reconciliations."""
        api = listing_api_url()
        all_rows = []
        pages_meta = []
        total = None
        seen = set()
        offset = 0
        while True:
            if not self.page_guard():
                raise ValueError(f"Fractal {label} listing exceeded the page budget at offset {offset}")
            payload = {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": offset, "searchText": ""}
            data = await self.http.json(api, method="POST", json=payload)
            rows = self._listing_rows(data, label)
            reported = data.get("total")
            if total is None:
                if not isinstance(reported, int) or reported < 0:
                    raise ValueError(f"Fractal {label} listing first page has no valid total: {reported!r}")
                total = reported
            elif reported not in (0, total):
                # Workday emits total=0 after the first page; any other drift is real.
                raise ValueError(f"Fractal {label} listing total changed: {total} -> {reported}")
            for row in rows:
                if row["externalPath"] in seen:
                    raise ValueError(f"Fractal {label} listing repeats path across pages: {row['externalPath']}")
                seen.add(row["externalPath"])
            all_rows.extend(rows)
            pages_meta.append({
                "offset": offset, "reported_total": reported,
                "rows_on_page": len(rows), "unique_cumulative": len(seen),
            })
            if len(seen) >= total:
                break
            if not rows or offset + len(rows) >= total + PAGE_LIMIT:
                raise ValueError(
                    f"Fractal {label} pagination incomplete at offset {offset}/{total}"
                )
            offset += len(rows)
        if len(seen) != total:
            raise ValueError(f"Fractal {label} pages reconcile to {len(seen)}, not total {total}")
        if total == 0:
            raise ValueError("Fractal official listing returned zero postings; empty state requires independent review")
        return {"rows": all_rows, "total": total, "pages": pages_meta}

    def _make_job(self, row, detail, *, api, detail_url, stale=False):
        info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
        if not isinstance(info, dict):
            raise ValueError(f"Fractal detail for {row['externalPath']} has no jobPostingInfo")
        path = row["externalPath"]
        expected_url = job_url(path)
        expected_detail_url = detail_api_url(path)
        if detail_url != expected_detail_url:
            raise ValueError(f"Fractal detail URL drifted: {detail_url} != {expected_detail_url}")
        title = info.get("title") or row.get("title")
        if info.get("title") and row.get("title") and info["title"] != row["title"]:
            self.result.errors.append(f"{path}: detail title differs from listing title")
        description_html = info.get("jobDescription") or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{path}: Fractal public jobDescription is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{path}: Fractal public jobDescription is implausibly short")
        external_url = info.get("externalUrl")
        if external_url not in (None, expected_url):
            self.result.errors.append(f"{path}: detail externalUrl differs from the canonical listed URL")
        apply_url = external_url or expected_url
        start_date = info.get("startDate")
        date_posted = start_date if isinstance(start_date, str) and _ISO_DATE.match(start_date.strip()) else None
        if start_date is not None and date_posted is None:
            self.result.errors.append(f"{path}: startDate is not an ISO date; kept null rather than fabricating")
        locations = []
        if info.get("location"):
            locations.append(info["location"])
        locations.extend(info.get("additionalLocations") or [])
        if not locations and row.get("locationsText"):
            locations = [row["locationsText"]]
        org = detail.get("hiringOrganization") or {}
        employer_name = (org.get("name") or "").strip() if isinstance(org, dict) else ""
        salary_keys = sorted(k for k in info if _SALARY_KEY.search(str(k)))
        if salary_keys:
            self.result.errors.append(f"{path}: unexpected salary keys in detail payload: {salary_keys}")
        attachment_links = job_attachment_links(description_html)
        raw = {
            "listing": row,
            "detail": detail,
            "source_ui": {
                "native_job_url": expected_url,
                "apply_url": apply_url,
                "listing_api_url": api,
                "detail_api_url": expected_detail_url,
                "job_posting_id": info.get("jobPostingId"),
                "requisition_id": info.get("jobReqId"),
                "hiring_organization": org.get("name") if isinstance(org, dict) else None,
                "can_apply": info.get("canApply"),
                "posted_flag": info.get("posted"),
                "attachment_links": attachment_links,
                "salary_keys_in_detail": salary_keys,
                "exposed_detail_keys": sorted(info.keys()),
            },
        }
        job = self.job(
            raw,
            title=title,
            url=expected_url,
            job_id=path,
            requisition_id=info.get("jobReqId"),
            apply_url=apply_url,
            description=description_html,
            method="fractal.workday.jobPostingInfo.jobDescription",
            description_url=expected_detail_url,
            locations=locations,
            employment_type=info.get("timeType"),
            workplace_type=info.get("remoteType"),
            employer=employer_name or None,
            date_posted=date_posted,
            date_posted_raw=info.get("postedOn"),
            date_updated=None,
            valid_through=info.get("endDate"),
            salary=None,
        )
        job.description_text = description_text
        job.record_identity = f"fractal:{path}"
        if stale:
            job.warnings.append("membership_changed_during_collection")
        if attachment_links:
            job.warnings.append("role_attachment_links_present")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in {"fractal.ai", "www.fractal.ai"}
                or parts.path.rstrip("/") != "/careers"):
            raise ValueError("Fractal dedicated flow must start from its official HTTPS /careers/ entry")
        entry = await self.http.get(entry_url)
        try:
            assert_listing_body(entry.text)
        except ValueError as exc:
            raise ValueError(f"Fractal official entry shape changed: {exc}") from exc
        origin, tenant, site = derive_workday_site(str(entry.url), entry.text)
        if (origin, tenant, site) != (ORIGIN, EXPECTED_TENANT, EXPECTED_SITE):
            raise ValueError(f"Fractal entry resolves to an unexpected site: {origin}/{tenant}/{site}")
        api = listing_api_url()

        listing_started = time.perf_counter()
        initial = await self._listing_snapshot("initial")
        listing_seconds = time.perf_counter() - listing_started
        initial_rows = initial["rows"]
        initial_ordered = [row["externalPath"] for row in initial_rows]
        initial_by_path = {row["externalPath"]: row for row in initial_rows}

        # Listing-stage jobs; details hydrate each exact listed path.
        listing_jobs = {}
        for row in initial_rows:
            job = self.job(
                {"listing": row},
                title=row["title"],
                url=job_url(row["externalPath"]),
                job_id=row["externalPath"],
                requisition_id=None,
            )
            job.record_identity = f"fractal:{row['externalPath']}"
            listing_jobs[row["externalPath"]] = job

        async def detail(job):
            path = job.job_id
            url = detail_api_url(path)
            payload = await self.http.json(url)
            detail_url = url
            return self._make_job(job.raw["listing"], payload, api=api, detail_url=detail_url)

        hydrated = await self.hydrate(list(listing_jobs.values()), detail)
        self.result.jobs = []
        self.result.dirty_jobs = []
        self.result.seen = set()
        jobs_by_path = {}
        title_drift_refetches = 0
        for job in hydrated:
            if job.job_id in jobs_by_path:
                continue
            row = initial_by_path.get(job.job_id)
            if row is not None:
                previous = job.raw.get("listing") or {}
                title_drift = bool(row.get("title") and previous.get("title")
                                   and row["title"] != previous["title"])
                if title_drift and job.title != row["title"]:
                    # A reused detail body must never carry a stale title.
                    # Refetch only this exact listed path.
                    payload = await self.http.json(detail_api_url(job.job_id))
                    job = self._make_job(row, payload, api=api,
                                         detail_url=detail_api_url(job.job_id))
                    title_drift_refetches += 1
                else:
                    self._apply_listing_row(job, row)
            jobs_by_path[job.job_id] = job
            # Hydrated job objects already carry their provenance; register
            # them through the base collector without re-mapping.
            self.result.dirty_jobs.append(job)
        self.result.jobs = list(jobs_by_path.values())
        self.result.dirty_jobs = list(self.result.jobs)
        self.result.metadata["listing_row_refresh"] = {
            "listing_rows_checked": len(initial_rows),
            "title_drift_detail_refetches": title_drift_refetches,
        }

        final_snapshot_verified = True
        try:
            final = await self._listing_snapshot("final")
            final_rows = final["rows"]
        except Exception as exc:
            final_snapshot_verified = False
            final_rows = initial_rows
            final = {"rows": initial_rows, "total": initial["total"], "pages": initial["pages"]}
            self.result.errors.append(
                "Fractal final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_path = {row["externalPath"]: row for row in final_rows}
        final_ordered = [row["externalPath"] for row in final_rows]
        membership_changed = final_snapshot_verified and set(final_ordered) != set(initial_ordered)
        if membership_changed:
            self.result.errors.append("fractal_official_listing_membership_changed_between_initial_and_final_snapshots")
            # Retain rows seen in either snapshot, annotating stale
            # initial-only rows so usable evidence is not discarded.
            for path in set(initial_by_path) - set(final_by_path):
                existing = jobs_by_path.get(path)
                if existing is not None and "membership_changed_during_collection" not in existing.warnings:
                    existing.warnings.append("membership_changed_during_collection")
            self.result.listing_complete = False
        self.result.listing_complete = not self.result.errors
        self.result.expected_total = len(final_ordered)
        if membership_changed:
            # Observed final total is recorded, but the two published
            # snapshots disagree, so this run cannot certify completeness.
            self.result.listing_complete = False

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "entry_final_url": str(entry.url),
            "entry_body_shape": "redirect_widget" if '"widget"' in entry.text else "canonical_shell",
            "resolved_workday_origin": origin,
            "resolved_tenant": tenant,
            "resolved_site": site,
            "listing_api_url": api,
            "listing_request_body": {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": "<each page>", "searchText": ""},
            "membership_authority": "Fractal official entry's hosted Workday Careers site; its unfiltered public CXS listing (appliedFacets {}) at every site-enforced page of 20",
            "ui_pagination": "Six unfiltered CXS pages of 20 at offsets 0,20,40,60,80,100 for the probed total of 117; later pages report total=0 while returning rows.",
            "initial_total": initial["total"],
            "final_total": final["total"],
            "final_snapshot_verified": final_snapshot_verified,
            "initial_pages": initial["pages"],
            "final_pages": final["pages"],
            "initial_ordered_paths": initial_ordered,
            "final_ordered_paths": final_ordered,
            "source_filter_behavior": "appliedFacets is empty; facets observed in the first response are provenance only and are never applied as filters",
            "description_source": "Each exact listed path's jobPostingInfo.jobDescription viewed through its own CXS detail GET.",
            "exposed_detail_key_union": sorted({k for j in self.result.jobs for k in j.raw.get("detail", {}).get("jobPostingInfo", {}) if isinstance(j.raw.get("detail"), dict)}),
            "salary_fields_exposed": any(j.raw.get("source_ui", {}).get("salary_keys_in_detail") for j in self.result.jobs),
            "job_attachment_links": {j.job_id: j.raw["source_ui"]["attachment_links"]
                                     for j in self.result.jobs if j.raw.get("source_ui", {}).get("attachment_links")},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["source_snapshot_phase_seconds"] = round(listing_seconds, 6)
        return self.result
