from urllib.parse import urlsplit

from .base import Adapter


class Keka(Adapter):
    """Public active-job feed observed on the company-linked Keka careers frontend."""

    async def run(self):
        parsed = urlsplit(self.source.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        api = origin + "/careers/api/jobs/default/active"
        rows = await self.http.json(api)
        if not isinstance(rows, list):
            raise ValueError("Keka active-job feed schema changed")
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            if not all(k in row for k in ("id", "title", "description")):
                raise ValueError("Keka active posting is missing its identity or description field")
            self.add(self.job(row, title=row["title"], job_id=row["id"],
                url=f"{origin}/careers/jobdetails/{row['id']}", description=row["description"],
                method="keka.active.description", description_url=api,
                locations=row.get("jobLocations") or [], department=row.get("departmentName"),
                date_posted=row.get("publishedOn"), salary=row.get("salaryRange"), experience=row.get("experience")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.seen) == len(rows)
        return self.result
