"""Company-hosted listing feeds with per-job ATS redirects supplied by those feeds."""
from html import escape
import re
from urllib.parse import urlsplit

from .base import Adapter
from .workday import coordinates
from ..models import text_html


class Postman(Adapter):
    async def run(self):
        api = "https://www.postman.com/_mk-www-next/api-cache/careers-jobs.json"
        data = await self.http.json(api)
        rows = data["jobs"]
        self.result.pages, self.result.expected_total = 1, int(data["meta"]["total"])
        for row in rows:
            self.add(self.job(row, title=row["title"], job_id=row["id"], url=row["url"],
                              locations=[row.get("location")], department=row.get("department")))
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == self.result.expected_total

        async def detail(job):
            parsed = urlsplit(job.url)
            if not (parsed.hostname or "").endswith(".myworkdayjobs.com"):
                raise ValueError("Postman changed its job detail destination; add the observed provider")
            origin, tenant, site = coordinates(job.url)
            path = "/job/" + parsed.path.split("/job/", 1)[1]
            api = f"{origin}/wday/cxs/{tenant}/{site}{path}"
            data = await self.http.json(api)
            row = data["jobPostingInfo"]
            job.raw["detail"] = data
            job.description_html = row["jobDescription"]
            job.description_method, job.description_source_url = "postman.linked_workday.jobDescription", api
            job.requisition_id = row.get("jobReqId")
            job.date_posted, job.date_posted_raw = row.get("startDate"), row.get("postedOn")
            job.locations = [row.get("location")] + (row.get("additionalLocations") or [])
            job.employment_type = row.get("timeType")
            job.apply_url = row.get("externalUrl") or job.url
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result


class PhonePe(Adapter):
    async def run(self):
        api = "https://www.phonepe.com/apollo/job-postings/latest.json"
        data = await self.http.json(api)
        if len(data["results"]) != data["totalCount"]:
            self.result.errors.append("phonepe_feed_total_mismatch")
        # The company's feed also contains nonpublic records. Match its public career list only.
        rows = [r for r in data["results"] if r.get("status") == "PUBLIC" and r.get("applyUrl")]
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            job_id = urlsplit(row["applyUrl"]).path.rstrip("/").split("/")[-1].split("-")[0]
            self.add(self.job(row, title=row["title"], job_id=job_id, url=row["applyUrl"],
                apply_url=row["applyUrl"], locations=[row.get("location")], department=row.get("department"),
                employment_type=row.get("type")))
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)

        async def detail(job):
            p = urlsplit(job.url)
            parts = p.path.strip("/").split("/")
            if p.hostname != "jobs.smartrecruiters.com" or len(parts) < 2:
                raise ValueError("PhonePe changed job detail destination; add the observed provider")
            job_id = re.match(r"\d+", parts[1]).group()
            api = f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{job_id}"
            row = await self.http.json(api)
            job.raw["detail"] = row
            sections = row.get("jobAd", {}).get("sections", {})
            if not any(text_html(s.get("text", "")) for s in sections.values() if isinstance(s, dict)):
                job.warnings.append("source_description_missing")
            job.description_html = "\n".join(f"<h2>{escape(str(s.get('title', k)))}</h2>{s.get('text', '')}"
                for k, s in sections.items() if isinstance(s, dict))
            job.description_method, job.description_source_url = "phonepe.linked_smartrecruiters.full_sections", api
            job.date_posted = row.get("releasedDate")
            job.apply_url = row.get("applyUrl") or job.apply_url
            job.locations = [row.get("location")]
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result
