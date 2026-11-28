from datetime import datetime, timezone
from html import escape
from urllib.parse import urlsplit

from .base import Adapter


def epoch(value, divisor=1000):
    try:
        return datetime.fromtimestamp(float(value) / divisor, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class Greenhouse(Adapter):
    async def run(self):
        board = self.source.options.get("board") or urlsplit(self.source.url).path.strip("/").split("/")[0]
        base = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        data = await self.http.json(base, params={"content": "true", "pay_transparency": "true"})
        rows = data["jobs"]
        self.result.pages = 1
        self.result.expected_total = data.get("meta", {}).get("total", len(rows))
        for row in rows:
            self.add(self.job(row, title=row["title"], url=row["absolute_url"], job_id=row["id"],
                requisition_id=row.get("requisition_id"), description=row.get("content"),
                method="greenhouse.content", description_url=base + "?content=true&pay_transparency=true",
                locations=[row.get("location")] + row.get("offices", []),
                department=row.get("departments"), date_posted=row.get("first_published"),
                date_updated=row.get("updated_at"), salary=row.get("pay_input_ranges"),
                language=row.get("language")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.result.jobs) == self.result.expected_total
        return self.result


class Lever(Adapter):
    async def run(self):
        parsed = urlsplit(self.source.url)
        board = self.source.options.get("board") or parsed.path.strip("/").split("/")[0]
        host = "api.eu.lever.co" if ".eu.lever.co" in parsed.netloc else "api.lever.co"
        base = f"https://{host}/v0/postings/{board}"
        offset = 0
        while self.page_guard():
            rows = await self.http.json(base, params={"mode": "json", "limit": 100, "skip": offset})
            if not isinstance(rows, list):
                raise ValueError("Lever response is not a postings array")
            if not rows:
                self.result.expected_total = len(self.result.jobs)
                self.result.listing_complete = True
                break
            before = len(self.seen)
            for row in rows:
                # descriptionPlain alone omits requirements, benefits and closing text.
                desc = row.get("description") or escape(row.get("descriptionPlain") or "")
                for section in row.get("lists", []):
                    desc += f"<h2>{escape(section.get('text', ''))}</h2><ul>{section.get('content', '')}</ul>"
                desc += row.get("additional") or escape(row.get("additionalPlain") or "")
                desc += row.get("salaryDescription") or ""
                cat = row.get("categories", {})
                self.add(self.job(row, title=row["text"], url=row["hostedUrl"], job_id=row["id"],
                    apply_url=row.get("applyUrl"), description=desc, method="lever.full_sections",
                    description_url=f"{base}/{row['id']}?mode=json",
                    locations=cat.get("allLocations") or [cat.get("location")],
                    department=cat.get("department"), team=cat.get("team"),
                    employment_type=cat.get("commitment"), workplace_type=row.get("workplaceType"),
                    date_posted=epoch(row.get("createdAt")), salary=row.get("salaryRange")))
                if self.limit():
                    return self.result
            if len(self.seen) == before:
                self.result.errors.append("pagination_repeated_page")
                break
            offset += len(rows)
        return self.result


class Ashby(Adapter):
    async def run(self):
        board = self.source.options.get("board") or urlsplit(self.source.url).path.strip("/").split("/")[0]
        api = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        data = await self.http.json(api, params={"includeCompensation": "true"})
        rows = data["jobs"]
        self.result.pages = 1
        self.result.expected_total = len(rows)
        for row in rows:
            self.add(self.job(row, title=row["title"], url=row["jobUrl"],
                job_id=row.get("id") or row["jobUrl"].rstrip("/").split("/")[-1],
                apply_url=row.get("applyUrl"), description=row.get("descriptionHtml"),
                description_text=row.get("descriptionPlain") or "", method="ashby.descriptionHtml",
                description_url=api + "?includeCompensation=true",
                locations=[{"location": row.get("location"), "address": row.get("address")}]
                    + row.get("secondaryLocations", []), department=row.get("department"),
                team=row.get("team"), employment_type=row.get("employmentType"),
                workplace_type=row.get("workplaceType"), date_posted=row.get("publishedAt"),
                salary=row.get("compensation")))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.result.jobs) == self.result.expected_total
        return self.result


class SmartRecruiters(Adapter):
    async def run(self):
        board = self.source.options.get("board") or urlsplit(self.source.url).path.strip("/").split("/")[0]
        base = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
        offset = 0
        while self.page_guard():
            data = await self.http.json(base, params={"limit": 100, "offset": offset})
            self.result.expected_total = int(data["totalFound"])
            rows = data["content"]
            before = len(self.seen)
            for row in rows:
                self.add(self.job(row, title=row["name"], job_id=row["id"],
                    url=f"https://jobs.smartrecruiters.com/{board}/{row['id']}",
                    date_posted=row.get("releasedDate"), locations=[row.get("location")],
                    department=row.get("department"), employment_type=row.get("typeOfEmployment")))
                if self.limit():
                    break
            if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
                break
            if len(self.seen) >= self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("pagination_incomplete_or_repeated")
                break
            offset += len(rows)

        async def detail(job):
            api = f"{base}/{job.job_id}"
            row = await self.http.json(api)
            job.raw["detail"] = row
            sections = row.get("jobAd", {}).get("sections", {})
            job.description_html = "\n".join(
                f"<h2>{escape(str(s.get('title', k)))}</h2>{s.get('text', '')}"
                for k, s in sections.items() if isinstance(s, dict))
            job.description_method, job.description_source_url = "smartrecruiters.jobAd.sections", api
            job.apply_url = row.get("applyUrl")
            job.url = row.get("postingUrl") or job.url
            job.salary = row.get("compensation")
            job.experience = row.get("experienceLevel")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result


class Workable(Adapter):
    async def run(self):
        board = self.source.options.get("board") or urlsplit(self.source.url).path.strip("/").split("/")[0]
        base = f"https://apply.workable.com/api/v1/widget/accounts/{board}"
        data = await self.http.json(base, params={"details": "true"})
        rows = data["jobs"]
        self.result.expected_total, self.result.pages = len(rows), 1
        for row in rows:
            desc = row.get("description", "")
            for key in ("requirements", "benefits"):
                if row.get(key):
                    desc += f"<h2>{key.title()}</h2>{row[key]}"
            self.add(self.job(row, title=row["title"], url=row["url"],
                job_id=row.get("shortcode") or row.get("id"), description=desc,
                apply_url=row.get("application_url"), method="workable.widget.details",
                description_url=base + "?details=true", locations=[row.get("location")],
                department=row.get("department"), employment_type=row.get("employment_type"),
                date_posted=row.get("published_on")))
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings
        return self.result
