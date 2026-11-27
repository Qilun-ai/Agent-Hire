"""Cisco's own Phenom global list and native job pages.

Membership authority is Cisco's unfiltered global refineSearch response on
its own https://careers.cisco.com/widgets endpoint, as configured by the
company-owned #search-results# page reached from the official entry. The
company-published sitemap index is used only as a deterministic completeness
cross-check: its candidate job URLs that never appear in refineSearch are
probed and only excluded when the company answers HTTP 410 Gone.
descriptionTeaser, ml_* summaries, AI summaries, footers and map fields are
never treated as descriptions; only phApp.ddo.jobDetail.data.job.description
is the full body. Workday apply URLs are retained as published, never
submitted.
"""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html
from ..structured import embedded_page_object as page_object

PAGE_SIZE = 500
MAX_LISTING_PASSES = 6
REFNUM = "CISCISGLOBAL"
BASE_HOST = "careers.cisco.com"
BASE_URL = "https://careers.cisco.com/global/en/"
SEARCH_SUFFIX = "global/en/search-results"
WIDGET_ENDPOINT = "https://careers.cisco.com/widgets"
SITEMAP_INDEX = "https://careers.cisco.com/global/en/sitemap_index.xml"
REQ_SEQ = re.compile(r"^CISCISGLOBAL(\d+)EXTERNALENGLOBAL$")
SITEMAP_JOB = re.compile(r"/global/en/job/(CISCISGLOBAL\d+EXTERNALENGLOBAL)/")
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _salary_from_detail(detail):
    value = detail.get("compensationRange")
    if isinstance(value, str) and value.strip() and value.strip() != "-":
        return {"source_text": value.strip(), "source_field": "compensationRange"}
    return None


def _sections(description_text):
    """Verbatim section excerpts only; never synthesised text (or None)."""
    text = description_text or ""
    qualifications = None
    responsibilities = None
    start = text.find("Minimum Qualifications")
    if start >= 0:
        start += len("Minimum Qualifications")
        end = len(text)
        for marker in ("Preferred Qualifications", "Why Cisco", "Disclaimer",
                       "Equal Opportunity", "Real Life", "Cisco Connect"):
            found = text.find(marker, start)
            if found >= 0:
                end = min(end, found)
        qualifies = text[start:end].strip()
        if len(qualifies) >= 40:
            qualifications = qualifies
    for marker in ("Key Responsibilities", "Responsibilities", "What You'll Do"):
        probe = text.find(marker)
        if probe < 0:
            continue
        probe += len(marker)
        end = len(text)
        for stop in ("Minimum Qualifications", "Preferred Qualifications", "Why Cisco",
                     "Disclaimer", "Equal Opportunity", "Real Life", "Cisco Connect"):
            found = text.find(stop, probe)
            if found >= 0:
                end = min(end, found)
        block = text[probe:end].strip()
        if len(block) >= 40:
            responsibilities = block
            break
    return qualifications, responsibilities


class Cisco(Adapter):
    """Follow only Cisco's published search widget and sitemap cross-check."""

    VERIFICATION_REVISION = "cisco.native-phenom-widget-and-full-pages.1"

    def _listing_body(self, offset):
        return {
            "lang": self.config["locale"],
            "deviceType": self.config["deviceType"],
            "country": self.config["country"],
            "pageName": self.config["pageName"],
            "ddoKey": "refineSearch",
            "from": offset,
            "size": PAGE_SIZE,
            "global": True,
            "selected_fields": {},
            "all_fields": ["city", "state", "category", "type", "orgFunction", "country"],
            "clearAll": True,
            "keywords": "",
            "subsearch": "",
            "sortBy": "",
            "jobs": True,
            "counts": True,
        }

    async def listing(self, offset):
        if not self.page_guard():
            raise ValueError("Cisco listing page budget exhausted")
        body = self._listing_body(offset)
        response = await self.http.json(
            self.endpoint, method="POST", json=body,
            headers={"Referer": self.search_entry},
        )
        value = response.get("refineSearch") if isinstance(response, dict) else None
        if (not isinstance(value, dict) or value.get("status") != 200
                or not isinstance(value.get("totalHits"), int) or value["totalHits"] < 0
                or not isinstance(value.get("data"), dict)
                or not isinstance(value["data"].get("jobs"), list)):
            raise ValueError("Cisco native search returned an unknown schema or error")
        rows = value["data"]["jobs"]
        if value.get("hits") != len(rows) or len(rows) > PAGE_SIZE:
            raise ValueError("Cisco native listing count/page size does not reconcile")
        for row in rows:
            req_id = row.get("reqId") if isinstance(row, dict) else None
            seq = row.get("jobSeqNo") if isinstance(row, dict) else None
            match = REQ_SEQ.fullmatch(seq or "")
            if (not isinstance(req_id, str) or not req_id.isdigit()
                    or not match or match.group(1) != req_id or not row.get("title")):
                raise ValueError("Cisco listing row has a missing or inconsistent identity")
            if row.get("siteType") != "external":
                raise ValueError("Cisco listing row left the external company board")
        return rows, value["totalHits"]

    async def sitemap_candidates(self):
        index = await self.http.get(SITEMAP_INDEX)
        if str(index.url) != SITEMAP_INDEX:
            raise ValueError("Cisco sitemap index redirected away from its published URL")
        locs = re.findall(r"<loc>([^<]+)</loc>", index.text)
        files = [loc for loc in locs if loc.startswith(BASE_URL + "sitemap") and loc.endswith(".xml")]
        if len(files) != 3:
            raise ValueError(f"Cisco sitemap index changed: expected 3 files, found {len(files)}")
        candidates = {}
        counts = []
        for file_url in files:
            page = await self.http.get(file_url)
            if str(page.url) != file_url:
                raise ValueError(f"Cisco sitemap file redirected: {file_url}")
            urls = re.findall(r"<loc>([^<]+)</loc>", page.text)
            job_urls = [u for u in urls if "/global/en/job/" in u]
            counts.append({"sitemap": file_url, "urls": len(urls), "job_urls": len(job_urls)})
            for loc in job_urls:
                match = SITEMAP_JOB.search(loc)
                if not match:
                    raise ValueError(f"Cisco sitemap job URL has an unknown shape: {loc}")
                req_id = REQ_SEQ.fullmatch(match.group(1)).group(1)
                if req_id in candidates:
                    raise ValueError(f"Cisco sitemap repeats candidate {req_id}")
                candidates[req_id] = {"url": loc, "jobSeqNo": match.group(1)}
        return candidates, counts

    async def detail(self, job):
        response = await self.http.get(job.url)
        parsed = urlsplit(str(response.url))
        match = SITEMAP_JOB.search(str(response.url))
        if parsed.hostname != BASE_HOST or not match:
            raise ValueError("Cisco posting redirected away from its exact native job URL")
        if match.group(1) != job.raw["listing"]["jobSeqNo"]:
            raise ValueError("Cisco native detail URL does not match the listed jobSeqNo")
        config = page_object(response.text, "var phApp = phApp || ")
        ddo = page_object(response.text, "phApp.ddo = ")
        value = ddo.get("jobDetail", {})
        data = value.get("data", {}).get("job") if isinstance(value, dict) else None
        listing = job.raw["listing"]
        if (config.get("refNum") != REFNUM or not isinstance(data, dict)
                or value.get("status") != 200
                or data.get("reqId") != job.job_id or data.get("jobId") != job.job_id
                or data.get("jobSeqNo") != listing["jobSeqNo"]
                or data.get("refNum") != REFNUM
                or not str(data.get("companyName", "")).casefold().startswith("cisco")):
            raise ValueError("Cisco full native page does not match the company-listed posting")
        if data.get("title") != listing.get("title"):
            raise ValueError(
                f"Cisco title conflict listing={listing.get('title')!r} detail={data.get('title')!r}"
            )
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Cisco full native description field is missing")
        job.raw["detail"] = data
        job.raw["native_detail_url"] = job.url
        job.description_html = description
        job.description_method = "cisco.native_phapp.jobDetail.description"
        job.description_source_url = job.url
        job.title = data.get("title") or listing["title"]
        job.requisition_id = data.get("jobRequisitionId") or data.get("reqId")
        job.apply_url = data.get("applyUrl") or listing.get("applyUrl")
        multi = data.get("multi_location")
        if isinstance(multi, list) and multi:
            job.locations = [{"location": loc} for loc in multi]
        elif data.get("location"):
            job.locations = [{"location": data["location"]}]
        elif listing.get("location"):
            job.locations = [{"location": listing["location"]}]
        job.department = data.get("department") or listing.get("department")
        job.employment_type = data.get("type") or listing.get("type")
        job.workplace_type = data.get("RemoteType")
        job.experience = None
        job.education = None
        job.date_posted = data.get("postedDate") or listing.get("postedDate")
        job.date_posted_raw = job.date_posted
        job.date_updated = data.get("jobUpdatedDate") or None
        job.valid_through = data.get("jobPostingEndDate") or None
        job.salary = _salary_from_detail(data)
        job.employer = data.get("companyName")
        job.language = data.get("locale")
        description_text = text_html(description)
        job.description_text = description_text
        qualifications, responsibilities = _sections(description_text)
        job.qualifications = qualifications
        job.responsibilities = responsibilities
        evergreen = str(data.get("IsEvergreenJob", "")).strip().lower() == "yes" or data.get("isEverGreenReq") == 1
        job.raw["source_ui"] = {
            "listing_jobSeqNo": listing["jobSeqNo"],
            "native_detail_url": job.url,
            "rendered_apply_url": job.apply_url,
            "attachment_links": _attachment_links(description),
            "salary_from_detail": job.salary,
            "qualifications_excerpt": qualifications is not None,
            "responsibilities_excerpt": responsibilities is not None,
            "is_evergreen": evergreen,
            "posting_status": data.get("postingStatus"),
            "refNum": data.get("refNum"),
        }
        if evergreen:
            job.warnings.append("evergreen_pipeline_location")
        return job

    async def run(self):
        started = self.result._started_clock
        if not self.page_guard():
            return self.result
        entry = await self.http.get(self.company.careers_url)
        final_entry = str(entry.url)
        parsed_entry = urlsplit(final_entry)
        if parsed_entry.hostname not in {BASE_HOST, "jobs.cisco.com"}:
            raise ValueError("Cisco entry left the inspected company careers hosts")
        soup = BeautifulSoup(entry.text, "lxml")
        link = next((a for a in soup.select("a[href]")
                     if a.get_text(" ", strip=True).casefold() == "search results"), None)
        if link is None:
            raise ValueError("Cisco official entry has no inspected Search results listing link")
        # The entry's relative link is routed by the site's own SPA base
        # (https://careers.cisco.com/global/en/), not by RFC file resolution
        # against the slash-less landing URL. Resolve it the same way.
        search_base = final_entry if final_entry.endswith("/") else final_entry + "/"
        self.search_entry = urljoin(search_base, link["href"])
        parsed_search = urlsplit(self.search_entry)
        if parsed_search.hostname != BASE_HOST or parsed_search.path.rstrip("/") != "/" + SEARCH_SUFFIX:
            raise ValueError("Cisco Search results link left its inspected company listing")

        search_page = await self.http.get(self.search_entry)
        if str(search_page.url) != self.search_entry:
            raise ValueError("Cisco search-results page redirected away from its published URL")
        self.config = page_object(search_page.text, "var phApp = phApp || ")
        self.endpoint = self.config.get("widgetApiEndpoint")
        self.base = self.config.get("baseUrl", "")
        if (self.config.get("refNum") != REFNUM or self.endpoint != WIDGET_ENDPOINT
                or self.config.get("siteType") != "external" or self.base != BASE_URL):
            raise ValueError("Cisco native company/search configuration changed")
        search_ddo = page_object(search_page.text, "phApp.ddo = ")
        eager = search_ddo.get("eagerLoadRefineSearch", {})
        eager_total = eager.get("totalHits") if isinstance(eager, dict) else None
        if eager_total is not None and not isinstance(eager_total, int):
            raise ValueError("Cisco search-results embedded total is not an integer")

        listing_started = time.perf_counter()
        union = {}
        page_records = []
        total = None
        passes = 0
        while True:
            passes += 1
            offsets = [0]
            if total is not None:
                offsets += list(range(PAGE_SIZE, total, PAGE_SIZE))
            pass_total = total
            for offset in (offsets if total is not None else [0]):
                rows, seen_total = await self.listing(offset)
                if pass_total is None:
                    pass_total = seen_total
                elif seen_total != pass_total:
                    self.result.errors.append("cisco_native_total_changed_during_pagination")
                record = {"pass": passes, "offset": offset, "hits": len(rows), "totalHits": seen_total}
                new_ids, seen_ids = [], []
                for row in rows:
                    seen_ids.append(row["reqId"])
                    if row["reqId"] not in union:
                        union[row["reqId"]] = row
                        new_ids.append(row["reqId"])
                record.update({"new_ids": len(new_ids), "duplicate_ids": len(seen_ids) - len(new_ids)})
                page_records.append(record)
                if total is None:
                    total = seen_total
            if total is not None and len(union) == total:
                break
            if passes >= MAX_LISTING_PASSES:
                self.result.errors.append(
                    f"cisco_native_listing_union_stalled_at_{len(union)}_of_{total}_after_{passes}_passes"
                )
                break
        listing_seconds = time.perf_counter() - listing_started

        sitemap_started = time.perf_counter()
        candidates, sitemap_counts = await self.sitemap_candidates()
        sitemap_seconds = time.perf_counter() - sitemap_started

        gone, live_not_listed, unlisted_candidate_ids = [], [], []
        for req_id, candidate in sorted(candidates.items()):
            if req_id in union:
                if candidate["jobSeqNo"] != union[req_id]["jobSeqNo"]:
                    self.result.errors.append(f"{req_id}: sitemap jobSeqNo differs from listing")
                continue
            unlisted_candidate_ids.append(req_id)
            try:
                response = await self.http.get(candidate["url"])
                live_not_listed.append({"reqId": req_id, "status": response.status_code})
                self.result.errors.append(
                    f"{req_id}: live sitemap candidate missing from company refineSearch list"
                )
            except Exception as exc:
                if "HTTP 410" in str(exc):
                    gone.append(req_id)
                else:
                    live_not_listed.append({"reqId": req_id, "error": str(exc)})
                    self.result.errors.append(
                        f"{req_id}: sitemap candidate probe failed inconclusively: {exc}"
                    )
        missing_from_sitemap = sorted(set(union) - set(candidates))
        for req_id in missing_from_sitemap:
            self.result.errors.append(f"{req_id}: refineSearch ID absent from company sitemap")

        if total is not None and eager_total is not None and eager_total != total:
            self.result.errors.append(
                f"cisco_search_page_total_{eager_total}_differs_from_widget_total_{total}"
            )

        for req_id in sorted(union):
            row = union[req_id]
            candidate = candidates.get(req_id)
            job_url = candidate["url"] if candidate else urljoin(
                self.base, "job/" + row["jobSeqNo"]
            )
            self.add(self.job(
                {"listing": row},
                title=row["title"], job_id=req_id, requisition_id=req_id, url=job_url,
                date_posted=row.get("postedDate"), date_posted_raw=row.get("postedDate"),
                apply_url=row.get("applyUrl"),
            ))
        self.result.expected_total = total
        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        final_ids = set()
        final_total = None
        offset = 0
        while final_total is None or offset < final_total:
            rows, final_total = await self.listing(offset)
            if final_total != total:
                self.result.errors.append("cisco_native_total_changed_before_final_proof")
            for row in rows:
                final_ids.add(row["reqId"])
            offset += len(rows)
            if not rows:
                break
        final_seconds = time.perf_counter() - final_started
        if final_ids != set(union):
            self.result.errors.append(
                f"cisco_native_final_proof_id_set_differs union_only={len(set(union) - final_ids)} "
                f"proof_only={len(final_ids - set(union))}"
            )

        self.result.pages = max(1, len(page_records))
        attachments_total = sum(len((job.raw.get("source_ui") or {}).get("attachment_links") or [])
                                for job in self.result.jobs)
        evergreen_ids = [job.job_id for job in self.result.jobs
                         if (job.raw.get("source_ui") or {}).get("is_evergreen")]
        self.result.listing_complete = (
            not self.result.errors and total is not None and len(union) == total
            and final_ids == set(union)
        )
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": final_entry,
            "native_search_entry": self.search_entry,
            "listing_authority": (
                "Cisco unfiltered global refineSearch on its own widgets endpoint "
                "(refNum CISCISGLOBAL, clearAll=true, global=true); company sitemap "
                "used only as a deterministic completeness cross-check"
            ),
            "widget_api_endpoint": self.endpoint,
            "refNum": REFNUM,
            "page_size": PAGE_SIZE,
            "eager_load_total_hits": eager_total,
            "widget_total_hits": total,
            "listing_passes": passes,
            "listing_pages": page_records,
            "sitemap_index_url": SITEMAP_INDEX,
            "sitemap_file_counts": sitemap_counts,
            "sitemap_candidate_count": len(candidates),
            "sitemap_gone_candidate_ids": gone,
            "live_sitemap_but_unlisted": live_not_listed,
            "listed_ids_absent_from_sitemap": missing_from_sitemap,
            "part_time_count": sum(1 for row in union.values() if row.get("type") == "Part time"),
            "evergreen_job_ids": evergreen_ids,
            "attachment_link_count": attachments_total,
            "final_proof_ids": len(final_ids),
            "listing_union_ids": len(union),
            "description_source": "phApp.ddo.jobDetail.data.job.description on each exact native detail page",
            "date_policy": "postedDate is the source posting date; dateCreated and template dates are retained only in raw.",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["sitemap_phase_seconds"] = round(sitemap_seconds, 6)
        self.result.timings["final_proof_phase_seconds"] = round(final_seconds, 6)
        self.result.timings["detail_wall_seconds"] = round(detail_seconds, 6)
        return self.result
