from urllib.parse import urljoin, urlsplit

from .base import Adapter
from .public_ats import epoch


class PCSX(Adapter):
    """Eightfold's current PCSX frontend, discovered from company page configuration."""

    async def run(self):
        p = urlsplit(self.source.url)
        origin = f"{p.scheme}://{p.netloc}"
        base = origin + "/api/pcsx"
        domain = self.source.options["domain"]
        offset = 0
        while self.page_guard():
            response = await self.http.json(base + "/search", params={
                "domain": domain, "query": "", "location": "", "start": offset, "sort_by": "timestamp"},
                headers={"Referer": self.source.url})
            data = response["data"]
            if not isinstance(data, dict) or "positions" not in data:
                raise ValueError(f"PCSX search failure: {response.get('error')}")
            rows = data["positions"]
            self.result.expected_total = int(data["count"])
            before = len(self.seen)
            for row in rows:
                self.add(self.job(row, title=row["name"], job_id=row["id"],
                    requisition_id=row.get("displayJobId") or row.get("atsJobId"),
                    url=urljoin(origin, row["positionUrl"]), locations=row.get("locations", []),
                    department=row.get("department"), date_posted=epoch(row.get("postedTs"), 1),
                    workplace_type=row.get("workLocationOption")))
                if self.limit():
                    break
            if "sample_limit" in self.result.warnings:
                break
            if len(self.seen) == self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("pcsx_pagination_count_mismatch")
                break
            offset += len(rows)

        async def detail(job):
            api = base + "/position_details"
            params = {"position_id": job.job_id, "domain": domain, "hl": "en"}
            response = await self.http.json(api, params=params, headers={"Referer": job.url})
            row = response["data"]
            if not isinstance(row, dict) or not row.get("jobDescription"):
                raise ValueError(f"PCSX full description missing: {response.get('error')}")
            job.raw["detail"] = row
            job.description_html = row["jobDescription"]
            job.description_method = "pcsx.position_details.jobDescription"
            job.description_source_url = api + f"?position_id={job.job_id}&domain={domain}&hl=en"
            job.apply_url = row.get("publicUrl")
            job.employment_type = row.get("efcustomTextEmploymentType")
            job.locations = row.get("locations", job.locations)
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result
