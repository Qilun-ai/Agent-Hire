from urllib.parse import urljoin, urlsplit

from .base import Adapter


class Jibe(Adapter):
    """Company-branded iCIMS/Jibe career frontends, e.g. AMD and GitHub."""

    async def run(self):
        parsed = urlsplit(self.source.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        prefix = parsed.path.rstrip("/").rsplit("/jobs", 1)[0]
        page = 1
        while self.page_guard():
            api = origin + "/api/jobs"
            response = await self.http.json(api, params={"page": page})
            total = int(response["totalCount"])
            if self.result.expected_total is None:
                self.result.expected_total = total
            elif total != self.result.expected_total:
                self.result.warnings.append("jibe_total_changed_during_crawl")
            rows = response["jobs"]
            before = len(self.seen)
            for item in rows:
                row = item["data"]
                # Jibe descriptions can exclude qualifications/responsibilities, so retain them separately too.
                description = row.get("description", "")
                for field in ("responsibilities", "qualifications", "benefits"):
                    value = row.get(field)
                    if value and value not in description:
                        description += f"<h2>{field.title()}</h2>{value}"
                job_url = row.get("job_url") or urljoin(origin, f"{prefix}/jobs/{row['slug']}")
                self.add(self.job(row, title=row["title"], url=job_url, job_id=row.get("req_id") or row["slug"],
                    description=description, method="jibe.full_fields", description_url=f"{api}?page={page}",
                    apply_url=row.get("apply_url"), locations=row.get("locations") or [{
                        "name": row.get("location_name"), "city": row.get("city"), "state": row.get("state"),
                        "country": row.get("country"), "full_location": row.get("full_location")}],
                    department=row.get("department") or row.get("categories"),
                    employment_type=row.get("employment_type"), date_posted=row.get("posted_date"),
                    date_updated=row.get("update_date"), qualifications=row.get("qualifications"),
                    responsibilities=row.get("responsibilities"), employer=row.get("hiring_organization")))
                if self.limit():
                    return self.result
            if len(self.seen) == self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("jibe_pagination_count_mismatch")
                break
            page += 1
        return self.result
