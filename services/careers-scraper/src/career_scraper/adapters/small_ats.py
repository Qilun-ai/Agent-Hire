from html import escape
from xml.etree import ElementTree

from .base import Adapter


class Recruitee(Adapter):
    async def run(self):
        base = self.source.url.rstrip("/")
        api = base + "/api/offers/"
        data = await self.http.json(api)
        rows = data["offers"]
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            desc = row.get("description", "")
            if row.get("requirements"):
                desc += "<h2>Requirements</h2>" + row["requirements"]
            self.add(self.job(row, title=row["title"], job_id=row["id"], url=row.get("careers_url") or f"{base}/o/{row['slug']}",
                description=desc, method="recruitee.description_and_requirements", description_url=api,
                locations=row.get("locations") or [row.get("location")], department=row.get("department"),
                employment_type=row.get("employment_type_code"), date_posted=row.get("published_at"),
                salary=row.get("salary")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.seen) == len(rows)
        return self.result


class Breezy(Adapter):
    async def run(self):
        api = self.source.url.rstrip("/") + "/json"
        rows = await self.http.json(api)
        if not isinstance(rows, list):
            raise ValueError("Expected Breezy positions array")
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            self.add(self.job(row, title=row["name"], job_id=row["id"], url=row["url"],
                description=row.get("description", ""), method="breezy.description", description_url=api,
                locations=[row.get("location")], department=row.get("department"),
                employment_type=row.get("type"), date_posted=row.get("published_date")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.seen) == len(rows)
        return self.result


class Personio(Adapter):
    async def run(self):
        api = self.source.url.rstrip("/") + "/xml"
        response = await self.http.get(api)
        root = ElementTree.fromstring(response.text)
        rows = root.findall("position")
        self.result.pages, self.result.expected_total = 1, len(rows)
        for node in rows:
            row = {child.tag: child.text for child in node if child.tag != "jobDescriptions"}
            desc = "".join(f"<h2>{escape(s.findtext('name') or '')}</h2>{s.findtext('value') or ''}"
                           for s in node.findall("jobDescriptions/jobDescription"))
            row["jobDescriptions"] = desc
            self.add(self.job(row, title=row.get("name"), job_id=row.get("id"),
                url=f"{self.source.url.rstrip('/')}/job/{row['id']}", description=desc,
                method="personio.jobDescriptions", description_url=api, locations=[row.get("office")],
                department=row.get("department"), employment_type=row.get("employmentType"),
                date_posted=row.get("createdAt")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.seen) == len(rows)
        return self.result
