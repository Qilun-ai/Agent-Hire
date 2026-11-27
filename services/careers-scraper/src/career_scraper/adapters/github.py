"""GitHub's own careers-home listing and exact native detail journey.

Membership authority is the unfiltered public board behind
https://www.github.careers/careers-home/jobs: GET /api/jobs with
internal=false (the shipped search bundle's default), 10 rows per page.
iCIMS login links in each row are apply destinations only; this
implementation never enumerates an iCIMS account or the internal board.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


ENTRY = "https://www.github.careers/careers-home/jobs"
API = "https://www.github.careers/api/jobs"


def canonical_text(html):
    """Ignore HTML entity spelling and inline-element whitespace only."""
    return re.sub(r"[\s\ufeff]", "", BeautifulSoup(html or "", "lxml").get_text())


def native_url(row):
    return f"{ENTRY}/{quote(str(row['slug']), safe='')}"


def source_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("GitHub source posting/update date is not a timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("GitHub source timestamp is missing its timezone")
    return stamp.isoformat()


def exact_native_fields(html, identity):
    soup = BeautifulSoup(html, "lxml")
    configs = [script.get_text() for script in soup.select("script")
               if "window.jobDescriptionConfig =" in script.get_text()]
    if len(configs) != 1:
        raise ValueError("GitHub exact native detail configuration is missing or ambiguous")
    content = configs[0].split("window.jobDescriptionConfig =", 1)[1].lstrip()
    config, _ = json.JSONDecoder().raw_decode(content)
    row = config.get("job", {})
    if (str(row.get("slug")) != str(identity) or row.get("client_code") != "githubinc"
            or not row.get("title") or config.get("sectionOrder") != ["description"]):
        raise ValueError("GitHub exact native detail identity, branding or full-body layout changed")
    schemas = [json.loads(n.get_text()) for n in soup.select('script[type="application/ld+json"]')]
    schemas = [s for s in schemas if isinstance(s, dict) and s.get("@type") == "JobPosting"]
    if len(schemas) != 1 or schemas[0].get("title") != row["title"]:
        raise ValueError("GitHub native JobPosting title does not match its exact detail")
    if canonical_text(schemas[0].get("description")) != canonical_text(row.get("description")):
        raise ValueError("GitHub native full body and JobPosting description disagree")
    return row, schemas[0], config["jdSettings"]


class GitHub(Adapter):
    VERIFICATION_REVISION = "github.official-careers-home-unfiltered-listing-full-fields.1"
    PAGE_SIZE = 10  # Observed displayLimit / UI page size for this company.

    def validate_row(self, row):
        if (not isinstance(row, dict) or row.get("client_code") != "githubinc"
                or not re.fullmatch(r"\d+", str(row.get("slug", "")))
                or not row.get("req_id") or not row.get("title")
                or not isinstance(row.get("language"), str)):
            raise ValueError("GitHub official listing contains an unidentified or different-company posting")
        if str(row["slug"]) != str(row["req_id"]):
            raise ValueError("GitHub official listing slug/req_id identity changed")
        if row.get("language") != "en-us":
            raise ValueError("GitHub official listing language changed from its inspected public board")
        apply = urlsplit(row.get("apply_url", ""))
        if (apply.scheme != "https" or apply.hostname not in {
                "careers-githubinc.icims.com", "globalcareers-githubinc.icims.com"}
                or apply.path != f"/jobs/{row['slug']}/login"
                or apply.query or apply.fragment):
            raise ValueError("GitHub listing's exact apply identity or inspected destination changed")
        source_date(row.get("posted_date"))
        source_date(row.get("update_date"))

    async def snapshot(self, phase):
        started = time.perf_counter()
        rows, ids, pages, total = [], set(), [], None
        page = 1
        while True:
            if not self.page_guard():
                raise ValueError("GitHub native listing page budget exhausted")
            value = await self.http.json(API, params={"internal": "false", "page": page})
            current = value.get("totalCount")
            items = value.get("jobs")
            if (not isinstance(current, int) or isinstance(current, bool) or current < 0
                    or not isinstance(items, list)):
                raise ValueError("GitHub official listing count or rows schema changed")
            if total is None:
                total = current
            elif current != total:
                raise ValueError("GitHub official listing count changed between pages")
            expected_size = min(self.PAGE_SIZE, max(0, total - (page - 1) * self.PAGE_SIZE))
            if len(items) != expected_size:
                raise ValueError(f"GitHub official listing page {page} has {len(items)} rows, expected {expected_size}")
            page_ids = []
            for item in items:
                row = item.get("data") if isinstance(item, dict) else None
                self.validate_row(row)
                identity = (str(row["slug"]), row["language"])
                if identity in ids:
                    raise ValueError(f"GitHub native listing repeats posting {identity}")
                ids.add(identity)
                rows.append(row)
                page_ids.append(list(identity))
            pages.append({"page": page, "count": len(items), "ids": page_ids,
                          "url": f"{API}?internal=false&page={page}"})
            if len(rows) == total:
                break
            page += 1
        self.result.metadata.setdefault("listing_snapshots", []).append(
            {"phase": phase, "total": total, "pages": pages})
        self.result.timings[f"{phase}_listing_seconds"] = time.perf_counter() - started
        return rows

    def full_job(self, row, page):
        identity = str(row["slug"])
        body = row.get("description")
        if not isinstance(body, str):
            body = ""
        primary = {k: row.get(k) for k in ("location_name", "city", "state", "country",
                                           "country_code", "full_location", "location_type",
                                           "latitude", "longitude")}
        locations = [primary, *(row.get("additional_locations") or [])]
        # Source structured salaries are zero template defaults (0/0/0);
        # there is no labelled hiring-target salary for this company.
        salary = None
        job = self.job({"listing": row}, title=row["title"], job_id=identity,
                       requisition_id=str(row["req_id"]), url=native_url(row),
                       apply_url=row["apply_url"], description=body,
                       method="github.native_listing.full_description",
                       description_url=f"{API}?internal=false&page={page}",
                       locations=locations, department=row.get("department") or row.get("categories"),
                       employment_type=row.get("employment_type"), employer=row.get("hiring_organization"),
                       language=row["language"], salary=salary,
                       date_posted=source_date(row.get("posted_date")), date_posted_raw=row.get("posted_date"),
                       date_updated=source_date(row.get("update_date")),
                       valid_through=None,
                       qualifications=row.get("qualifications"), responsibilities=row.get("responsibilities"))
        job.record_identity = f"github:{identity}"
        # The native layout renders precisely `description`. Separate source
        # fields are retained in place, without duplicating footer text.
        description_text = canonical_text(body)
        missing = [key for key in ("responsibilities", "qualifications")
                   if canonical_text(row.get(key)) and canonical_text(row[key]) not in description_text]
        if missing:
            job.warnings.append("description_not_full_field")
            job.raw["sections_not_in_description"] = missing
        attachments = [urljoin(job.url, a["href"]) for a in BeautifulSoup(body, "lxml").select("a[href]")
                       if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", a["href"], re.I)]
        if attachments:
            job.raw["published_attachment_urls"] = list(dict.fromkeys(attachments))
            job.warnings.append("description_not_full_field")
            self.result.errors.append(f"GitHub {identity}: published role attachment requires exact-document extraction: {attachments}")
        return job

    async def check_exact_detail(self, job):
        identity = (job.job_id, job.language)
        if identity not in self.published_ids:
            raise ValueError("GitHub exact detail was not exposed by the official listing")
        response = await self.http.get(job.url)
        if urlsplit(str(response.url)).path != urlsplit(job.url).path:
            raise ValueError("GitHub exact detail redirected away from the listed posting")
        row, schema, settings = exact_native_fields(response.text, job.job_id)
        listing = job.raw["listing"]
        for key in ("title", "req_id", "language", "apply_url", "posted_date", "employment_type"):
            if row.get(key) != listing.get(key):
                raise ValueError(f"GitHub exact detail/listing {key} mismatch for {job.job_id}")
        for key in ("description", "responsibilities", "qualifications"):
            if canonical_text(row.get(key)) != canonical_text(listing.get(key)):
                raise ValueError(f"GitHub exact native {key} differs from its listed full field for {job.job_id}")
        job.raw["native_detail_sample"] = {"job": row, "schema": schema, "jdSettings": settings}
        return job

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text, "lxml")
        if (urlsplit(str(entry.url)).hostname != "www.github.careers"
                or urlsplit(str(entry.url)).path != "/careers-home/jobs"
                or not re.search(r'window\._jibe\s*=\s*\{\s*"cid"\s*:\s*"githubinc"', entry.text)
                or not any("app.jibecdn.com/prod/search/" in s.get("src", "") for s in soup.select("script[src]"))):
            raise ValueError("GitHub official native careers publisher or search application changed")
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION, official_entry=str(entry.url),
            listing_authority="GitHub's own unfiltered native careers listing at /careers-home/jobs",
            page_size_basis="Observed displayLimit 10; the shipped search bundle requests /api/jobs with internal=false and no limit override",
            date_semantics="posted_date is the public posting timestamp; create_date is importer creation and is not substituted. Without an explicit source expiry the valid_through stays null.",
            description_semantics="Full description is returned inline for every listed job; native sectionOrder is description. Separate responsibilities and qualifications are retained, not appended twice.",
            ats_account_enumeration_used=False)
        rows = await self.snapshot("initial")
        self.published_ids = {(str(row["slug"]), row["language"]) for row in rows}
        self.result.expected_total = len(rows)
        started = time.perf_counter()
        for index, row in enumerate(rows):
            self.add(self.full_job(row, index // self.PAGE_SIZE + 1))
            if self.limit():
                return self.result
        self.result.timings["inline_detail_extraction_seconds"] = time.perf_counter() - started
        # Native samples include the first listed posting, the last listed
        # posting, a global-careers apply host when present, and any job
        # whose role body failed the inline check; IDs are selected only
        # from this captured listing.
        selected = set()
        if self.result.jobs:
            selected.add(self.result.jobs[0].job_id)
            selected.add(self.result.jobs[-1].job_id)
            for job in self.result.jobs:
                if "globalcareers-githubinc.icims.com" in (job.apply_url or ""):
                    selected.add(job.job_id)
                    break
        selected.update(j.job_id for j in self.result.jobs if not j.description_verified)
        started = time.perf_counter()
        native_checked = []
        for job in self.result.jobs:
            if job.job_id not in selected:
                continue
            try:
                await self.check_exact_detail(job)
                native_checked.append(job.job_id)
            except Exception as exc:
                self.result.errors.append(f"GitHub exact native detail {job.job_id}: {type(exc).__name__}: {exc}")
                job.warnings.append("detail_fetch_failed")
                self.result.validate_job(job)
        self.result.timings["native_detail_reference_seconds"] = time.perf_counter() - started
        self.result.metadata["details"] = {"total": len(rows), "inline_full_fields_checked": len(rows),
                                            "exact_native_detail_ids_checked": native_checked,
                                            "verified_cache_hits": 0}
        final = await self.snapshot("final")
        final_ids = {(str(row["slug"]), row["language"]) for row in final}
        if final_ids != self.published_ids:
            self.result.errors.append("github_official_native_listing_membership_changed")
        else:
            current = {(j.job_id, j.language): index for index, j in enumerate(self.result.jobs)}
            refreshed_ids = []
            for index, row in enumerate(final):
                identity = (str(row["slug"]), row["language"])
                old = self.result.jobs[current[identity]]
                if old.raw["listing"] == row:
                    continue
                fresh = self.full_job(row, index // self.PAGE_SIZE + 1)
                fresh.raw["initial_listing"] = old.raw["listing"]
                if "native_detail_sample" in old.raw:
                    fresh.raw["native_detail_sample"] = old.raw["native_detail_sample"]
                    fresh.raw["native_detail_sample_basis"] = "Initial captured listing; final source fields retained separately"
                    if canonical_text(old.description_html) != canonical_text(fresh.description_html):
                        try:
                            await self.check_exact_detail(fresh)
                        except Exception as exc:
                            fresh.warnings.append("detail_fetch_failed")
                            self.result.errors.append(f"GitHub final full-body update {fresh.job_id}: {exc}")
                self.result.validate_job(fresh)
                self.result.jobs[current[identity]] = fresh
                self.result.dirty_jobs.append(fresh)
                refreshed_ids.append(fresh.job_id)
            self.result.metadata["final_snapshot_refreshed_ids"] = refreshed_ids
        self.result.listing_complete = final_ids == self.published_ids and not self.result.errors
        return self.result
