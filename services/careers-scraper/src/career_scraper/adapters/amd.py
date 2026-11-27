"""AMD's inspected, unfiltered native careers listing and exact native details.

The global/category links filter this same listing. iCIMS login links are apply
destinations only; this implementation never enumerates an iCIMS account.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


ENTRY = "https://careers.amd.com/careers-home/jobs"
API = "https://careers.amd.com/api/jobs"


def canonical_text(html):
    """Ignore HTML entity spelling and inline-element whitespace only."""
    return re.sub(r"[\s\ufeff]", "", BeautifulSoup(html or "", "lxml").get_text())


def native_url(row):
    return f"{ENTRY}/{quote(str(row['slug']), safe='')}?lang={quote(row['language'], safe='-')}"


def source_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("AMD source posting/update date is not a timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("AMD source timestamp is missing its timezone")
    return stamp.isoformat()


def exact_native_fields(html, identity):
    soup = BeautifulSoup(html, "lxml")
    configs = [script.get_text() for script in soup.select("script")
               if "window.jobDescriptionConfig =" in script.get_text()]
    if len(configs) != 1:
        raise ValueError("AMD exact native detail configuration is missing or ambiguous")
    content = configs[0].split("window.jobDescriptionConfig =", 1)[1].lstrip()
    config, _ = json.JSONDecoder().raw_decode(content)
    row = config.get("job", {})
    if (str(row.get("slug")) != str(identity) or row.get("client_code") != "amd"
            or not row.get("title") or config.get("sectionOrder") != ["description"]):
        raise ValueError("AMD exact native detail identity, branding or full-body layout changed")
    schemas = [json.loads(n.get_text()) for n in soup.select('script[type="application/ld+json"]')]
    schemas = [s for s in schemas if isinstance(s, dict) and s.get("@type") == "JobPosting"]
    if len(schemas) != 1 or schemas[0].get("title") != row["title"]:
        raise ValueError("AMD native JobPosting title does not match its exact detail")
    if canonical_text(schemas[0].get("description")) != canonical_text(row.get("description")):
        raise ValueError("AMD native full body and JobPosting description disagree")
    return row, schemas[0], config["jdSettings"]


class AMD(Adapter):
    VERIFICATION_REVISION = "amd.official-unfiltered-native-100-row-full-fields.3"
    PAGE_SIZE = 100  # One of the inspected UI's 5/10/25/100 choices.

    def validate_row(self, row):
        if (not isinstance(row, dict) or row.get("client_code") != "amd"
                or not re.fullmatch(r"\d+", str(row.get("slug", "")))
                or not row.get("req_id") or not row.get("title")
                or not isinstance(row.get("language"), str)):
            raise ValueError("AMD official listing contains an unidentified or different-company posting")
        apply = urlsplit(row.get("apply_url", ""))
        if (apply.scheme != "https" or not re.fullmatch(r"[a-z0-9-]*amd\.icims\.com", apply.hostname or "")
                or not apply.path.startswith(f"/jobs/{row['slug']}/")):
            raise ValueError("AMD listing's exact apply identity or inspected destination changed")
        source_date(row.get("posted_date"))
        source_date(row.get("update_date"))
        source_date(row.get("posting_expiry_date"))

    async def snapshot(self, phase):
        started = time.perf_counter()
        rows, ids, pages, total = [], set(), [], None
        page = 1
        while True:
            if not self.page_guard():
                raise ValueError("AMD native listing page budget exhausted")
            value = await self.http.json(API, params={"limit": self.PAGE_SIZE, "page": page})
            current = value.get("totalCount")
            items = value.get("jobs")
            if (not isinstance(current, int) or isinstance(current, bool) or current < 0
                    or not isinstance(items, list)):
                raise ValueError("AMD official listing count or rows schema changed")
            if total is None:
                total = current
            elif current != total:
                raise ValueError("AMD official listing count changed between pages")
            expected_size = min(self.PAGE_SIZE, max(0, total - (page - 1) * self.PAGE_SIZE))
            if len(items) != expected_size:
                raise ValueError(f"AMD official listing page {page} has {len(items)} rows, expected {expected_size}")
            page_ids = []
            for item in items:
                row = item.get("data") if isinstance(item, dict) else None
                self.validate_row(row)
                identity = (str(row["slug"]), row["language"])
                if identity in ids:
                    raise ValueError(f"AMD native listing repeats posting {identity}")
                ids.add(identity)
                rows.append(row)
                page_ids.append(list(identity))
            pages.append({"page": page, "count": len(items), "ids": page_ids,
                          "url": f"{API}?limit={self.PAGE_SIZE}&page={page}"})
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
        primary = {k: row.get(k) for k in ("location_name", "street_address", "city", "state", "country",
                                           "country_code", "postal_code", "latitude", "longitude",
                                           "full_location", "location_type")}
        locations = [primary, *(row.get("additional_locations") or [])]
        target_min, target_max = row.get("tags5"), row.get("tags6")
        salary_tags = {key: row[key] for key in ("tags2", "tags3", "tags5", "tags6", "tags7", "tags8") if key in row}
        # Zero USD amounts in JSON-LD are a template default even for Indian
        # roles. Only the native UI's labelled hiring target has that meaning.
        salary = {"hiring_target_min": target_min or None, "hiring_target_max": target_max or None,
                  "source_salary_tags": salary_tags} if salary_tags else None
        job = self.job({"listing": row}, title=row["title"], job_id=identity,
                       requisition_id=str(row["req_id"]), url=native_url(row),
                       apply_url=row["apply_url"], description=body,
                       method="amd.native_listing.full_description",
                       description_url=f"{API}?limit={self.PAGE_SIZE}&page={page}",
                       locations=locations, department=row.get("department") or row.get("categories"),
                       employment_type=row.get("employment_type"), employer=row.get("hiring_organization"),
                       language=row["language"], salary=salary,
                       date_posted=source_date(row.get("posted_date")), date_posted_raw=row.get("posted_date"),
                       date_updated=source_date(row.get("update_date")),
                       valid_through=source_date(row.get("posting_expiry_date")),
                       qualifications=row.get("qualifications"), responsibilities=row.get("responsibilities"))
        # The native layout renders precisely `description`. Separate source
        # fields remain separately available, without duplicating footer text.
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
            self.result.errors.append(f"AMD {identity}: published role attachment requires exact-document extraction: {attachments}")
        return job

    async def check_exact_detail(self, job):
        identity = (job.job_id, job.language)
        if identity not in self.published_ids:
            raise ValueError("AMD exact detail was not exposed by the official listing")
        response = await self.http.get(job.url)
        if urlsplit(str(response.url)).path != urlsplit(job.url).path:
            raise ValueError("AMD exact detail redirected away from the listed posting")
        row, schema, settings = exact_native_fields(response.text, job.job_id)
        listing = job.raw["listing"]
        for key in ("title", "req_id", "language", "apply_url", "posted_date", "employment_type"):
            if row.get(key) != listing.get(key):
                raise ValueError(f"AMD exact detail/listing {key} mismatch for {job.job_id}")
        for key in ("description", "responsibilities", "qualifications"):
            if canonical_text(row.get(key)) != canonical_text(listing.get(key)):
                raise ValueError(f"AMD exact native {key} differs from its listed full field for {job.job_id}")
        job.raw["native_detail_sample"] = {"job": row, "schema": schema, "jdSettings": settings}
        return job

    async def check_student_journey(self, rows):
        """The official student-program CTA is another view of this same list."""
        response = await self.http.get("https://careers.amd.com/students/jobs")
        scripts = BeautifulSoup(response.text, "lxml").select("script")
        configs = [n.get_text().split("window.searchConfig =", 1)[1].lstrip()
                   for n in scripts if "window.searchConfig =" in n.get_text()]
        if len(configs) != 1:
            raise ValueError("AMD published student listing configuration is missing")
        config, _ = json.JSONDecoder().raw_decode(configs[0])
        category = "Student / Intern / Temp"
        if unquote(config.get("query", {}).get("categories", "")) != category:
            raise ValueError("AMD student listing is no longer the inspected category view")
        if not self.page_guard():
            raise ValueError("AMD student-view request budget exhausted")
        value = await self.http.json(API, params={"limit": self.PAGE_SIZE, "page": 1, "categories": category})
        # A category can grow beyond one UI page. Follow its same published
        # 100-row pagination rather than assuming today's 99 rows forever.
        total, student_rows, page = value.get("totalCount"), [], 1
        while True:
            if not isinstance(total, int) or total < 0 or value.get("totalCount") != total:
                raise ValueError("AMD student category count changed")
            items = value.get("jobs")
            if not isinstance(items, list) or len(items) != min(self.PAGE_SIZE, max(0, total - len(student_rows))):
                raise ValueError("AMD student category pagination is incomplete")
            for item in items:
                row = item.get("data") if isinstance(item, dict) else None
                self.validate_row(row)
                student_rows.append(row)
            if len(student_rows) == total:
                break
            page += 1
            if not self.page_guard():
                raise ValueError("AMD student-view request budget exhausted")
            value = await self.http.json(API, params={"limit": self.PAGE_SIZE, "page": page, "categories": category})
        expected = {(str(r["slug"]), r["language"]) for r in rows
                    if any(c.get("name") == category for c in r.get("categories", []))}
        actual = [(str(r["slug"]), r["language"]) for r in student_rows]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise ValueError("AMD official student view does not reconcile with the full global listing")
        self.result.metadata["student_journey"] = {"entry": str(response.url), "category": category,
                                                  "total": total, "pages": page,
                                                  "ids": [list(i) for i in actual],
                                                  "global_listing_subset_verified": True}

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text, "lxml")
        if (urlsplit(str(entry.url)).hostname != "careers.amd.com"
                or not re.search(r'window\._jibe\s*=\s*\{\s*"cid"\s*:\s*"amd"', entry.text)
                or not any("app.jibecdn.com/prod/search/" in s.get("src", "") for s in soup.select("script[src]"))):
            raise ValueError("AMD official native careers publisher or search application changed")
        journeys = [{"text": a.get_text(" ", strip=True), "url": urljoin(str(entry.url), a["href"])}
                    for a in soup.select("a[href]") if "jobs?" in a["href"]]
        if not any("country=India" in link["url"] for link in journeys):
            raise ValueError("AMD inspected global filtering journey is missing")
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION, official_entry=str(entry.url),
            listing_authority="AMD's own unfiltered native careers listing; global/category journeys filter the same list",
            official_filtered_journeys=journeys, page_size_basis="Inspected UI Items per page choices 5/10/25/100",
            date_semantics="posted_date is the public posting timestamp; create_date is importer creation. Explicit posting_expiry_date is the source deadline; otherwise valid_through is null. A one-year JSON-LD fallback is not an actual deadline.",
            description_semantics="Full description is returned inline for every listed job; native sectionOrder is description. Separate responsibilities and qualifications are retained, not appended twice.",
            ats_account_enumeration_used=False)
        rows = await self.snapshot("initial")
        await self.check_student_journey(rows)
        self.published_ids = {(str(row["slug"]), row["language"]) for row in rows}
        self.result.expected_total = len(rows)
        self.result.metadata["explicit_source_posting_expiry_dates"] = sum(bool(row.get("posting_expiry_date")) for row in rows)
        started = time.perf_counter()
        for index, row in enumerate(rows):
            self.add(self.full_job(row, index // self.PAGE_SIZE + 1))
            if self.limit():
                return self.result
        self.result.timings["inline_detail_extraction_seconds"] = time.perf_counter() - started
        # Native samples include both iCIMS apply hosts, an intern, and the last
        # published posting; IDs are selected only from this captured listing.
        selected = {"92874", "94296", "90710"}
        if self.result.jobs:
            selected.add(self.result.jobs[-1].job_id)
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
                self.result.errors.append(f"AMD exact native detail {job.job_id}: {type(exc).__name__}: {exc}")
                job.warnings.append("detail_fetch_failed")
                self.result.validate_job(job)
        self.result.timings["native_detail_reference_seconds"] = time.perf_counter() - started
        self.result.metadata["details"] = {"total": len(rows), "inline_full_fields_checked": len(rows),
                                            "exact_native_detail_ids_checked": native_checked,
                                            "verified_cache_hits": 0}
        final = await self.snapshot("final")
        final_ids = {(str(row["slug"]), row["language"]) for row in final}
        if final_ids != self.published_ids:
            self.result.errors.append("amd_official_native_listing_membership_changed")
        else:
            # Live importer updates can leave membership/body unchanged while
            # modifying posting metadata during collection. The final listing
            # already carries the complete fields: retain the latest exact row
            # without requesting an unrelated board or repeating the crawl.
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
                            self.result.errors.append(f"AMD final full-body update {fresh.job_id}: {exc}")
                self.result.validate_job(fresh)
                self.result.jobs[current[identity]] = fresh
                self.result.dirty_jobs.append(fresh)
                refreshed_ids.append(fresh.job_id)
            self.result.metadata["final_snapshot_refreshed_ids"] = refreshed_ids
            self.result.metadata["explicit_source_posting_expiry_dates"] = sum(bool(j.valid_through) for j in self.result.jobs)
        self.result.listing_complete = final_ids == self.published_ids and not self.result.errors
        return self.result
