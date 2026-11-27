"""Public Gem boards linked by company careers sites, with batched full-description queries."""
from urllib.parse import quote, urlsplit

from .base import Adapter
from .public_ats import epoch
from ..models import text_html


class Gem(Adapter):
    API = "https://jobs.gem.com/api/public/graphql/batch"
    BATCH_SIZE = 50
    LIST = """query JobBoardList($boardId:String!){oatsExternalJobPostings(boardId:$boardId){jobPostings{
      id extId title locations{id name city isoCountry isRemote extId}
      job{id department{id name extId} locationType employmentType}}}}"""
    DETAIL = """query ExternalJobPostingQuery($boardId:String!,$extId:String!){
      oatsExternalJobPosting(boardId:$boardId,extId:$extId){extId firstPublishedTsSec descriptionHtml
      jobPostSectionHtml{introHtml outroHtml} compensationHtml}}"""

    async def queries(self, operations):
        if not self.page_guard():
            raise ValueError("Gem page budget exhausted")
        data = await self.http.json(self.API, method="POST", headers={"batch": "true"}, json=operations)
        if not isinstance(data, list) or len(data) != len(operations):
            raise ValueError("Gem batch response did not match the requested operation count")
        return data

    async def listing(self, board):
        result = (await self.queries([{"operationName": "JobBoardList", "variables": {"boardId": board}, "query": self.LIST}]))[0]
        if result.get("errors"):
            raise ValueError(f"Gem listing query failed: {result['errors']}")
        rows = ((result.get("data") or {}).get("oatsExternalJobPostings") or {}).get("jobPostings")
        if not isinstance(rows, list):
            raise ValueError("Gem public listing schema changed")
        return rows

    async def run(self):
        board = urlsplit(self.source.url).path.strip("/").split("/")[0]
        rows = await self.listing(board)
        self.result.expected_total = len(rows)
        original_ids = set()
        for row in rows:
            if not row.get("extId") or not row.get("title"):
                raise ValueError("Gem posting is missing its public identity/title")
            original_ids.add(row["extId"])
            info = row.get("job") or {}
            self.add(self.job(row, title=row["title"], job_id=row["extId"],
                url=f"https://jobs.gem.com/{quote(board, safe='')}/{quote(row['extId'], safe='')}",
                locations=row.get("locations") or [], department=info.get("department"),
                employment_type=info.get("employmentType"), workplace_type=info.get("locationType")))
            if self.limit():
                break
        for offset in range(0, len(self.result.jobs), self.BATCH_SIZE):
            batch = self.result.jobs[offset:offset + self.BATCH_SIZE]
            operations = [{"operationName": "ExternalJobPostingQuery", "variables": {"boardId": board, "extId": job.job_id},
                           "query": self.DETAIL} for job in batch]
            entries = await self.queries(operations)
            details = {}
            for entry in entries:
                if entry.get("errors"):
                    self.result.errors.append(f"Gem detail query failed: {entry['errors']}")
                posting = (entry.get("data") or {}).get("oatsExternalJobPosting")
                if isinstance(posting, dict) and posting.get("extId"):
                    details[posting["extId"]] = posting
            for job in batch:
                detail = details.get(job.job_id)
                if not detail:
                    job.warnings.append("detail_fetch_failed")
                    self.result.errors.append(f"gem_detail_missing: {job.job_id}")
                    continue
                job.raw["detail"] = detail
                sections = detail.get("jobPostSectionHtml") or {}
                body = detail.get("descriptionHtml") or ""
                job.description_html = "\n".join(x for x in [sections.get("introHtml"), body, sections.get("outroHtml"),
                    ("<h2>Compensation</h2>" + detail["compensationHtml"]) if detail.get("compensationHtml") else None] if x)
                job.description_method, job.description_source_url = "gem.external_posting.full_sections", self.API
                if "descriptionHtml" not in detail:
                    job.warnings.append("description_not_full_field")
                    self.result.errors.append(f"gem_detail_schema_missing_description: {job.job_id}")
                elif not text_html(body):
                    job.description_unavailable = True
                    job.warnings.append("source_description_missing")
                ts = detail.get("firstPublishedTsSec")
                job.date_posted = epoch(ts, 1) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 else None
                job.date_posted_raw = ts
                job.salary = {"html": detail["compensationHtml"]} if detail.get("compensationHtml") else None
                self.result.validate_job(job)
                self.result.dirty_jobs.append(job)
            if self.options.checkpoint:
                self.options.checkpoint(self.result)
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)
        if self.result.listing_complete:
            final = await self.listing(board)
            if {row.get("extId") for row in final} != original_ids:
                self.result.listing_complete = False
                self.result.errors.append("gem_listings_changed_during_run")
        return self.result
