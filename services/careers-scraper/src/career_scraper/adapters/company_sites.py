"""Handlers for company-owned listing and description pages, independent of ATS APIs."""
import json
import re
from html import escape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import Adapter
from .airbnb import Airbnb  # noqa: F401 — retained public import for existing callers
from .html import from_schema, job_postings
from .public_ats import epoch


def assignment_json(html, marker):
    start = html.index(marker) + len(marker)
    return json.JSONDecoder().raw_decode(html[start:].lstrip())[0]


def apple_state(html):
    return json.loads(assignment_json(html, "window.__staticRouterHydrationData = JSON.parse("))["loaderData"]


class Atlassian(Adapter):
    async def run(self):
        api = "https://www.atlassian.com/endpoint/careers/listings"
        rows = await self.http.json(api)
        if not isinstance(rows, list):
            raise ValueError("Atlassian careers listing schema changed")
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            desc = "\n".join(row.get(key, "") or "" for key in ("overview", "responsibilities", "qualifications"))
            job = self.job(row, title=row["title"], job_id=row["id"],
                url=row["portalJobPost"]["portalUrl"], apply_url=row.get("applyUrl"),
                description=desc, method="atlassian.careers.full_sections", description_url=api,
                locations=row.get("locations", []), department=row.get("category"),
                date_updated=row["portalJobPost"].get("updatedDate"),
                qualifications=row.get("qualifications"), responsibilities=row.get("responsibilities"))
            if not desc.strip():
                job.warnings.append("source_description_missing")
            self.add(job)
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.seen) == len(rows)
        return self.result


class Cred(Adapter):
    async def run(self):
        api = "https://kong-web-prod.dreamplug.in/lever-jobs-api/v1/jobs"
        data = await self.http.json(api)
        rows = data["data"]
        self.result.pages, self.result.expected_total = 1, len(rows)
        for row in rows:
            cat = row.get("categories", {})
            self.add(self.job(row, title=row["text"], job_id=row["id"], url=row["urls"]["show"],
                apply_url=row["urls"].get("apply"), locations=cat.get("allLocations") or [cat.get("location")],
                department=cat.get("department"), team=cat.get("team"),
                date_posted=epoch(row.get("createdAt")), workplace_type=row.get("workplaceType")))
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)

        async def detail(job):
            # The company's own listing explicitly supplies this Lever job URL.
            from ..discovery import detect
            source = detect(job.url)
            if source is None or source.adapter != "lever":
                raise ValueError("CRED changed its job-detail destination")
            board = source.url.rstrip("/").split("/")[-1]
            detail_url = f"https://api.lever.co/v0/postings/{board}/{job.job_id}"
            row = await self.http.json(detail_url)
            job.raw["detail"] = row
            parts = [row.get("description", "")]
            parts += [f"<h2>{escape(s.get('text', ''))}</h2><ul>{s.get('content', '')}</ul>" for s in row.get("lists", [])]
            parts += [row.get("additional", ""), row.get("salaryDescription", "")]
            job.description_html = "\n".join(parts)
            job.description_method, job.description_source_url = "cred.linked_lever.full_sections", detail_url
            job.salary = row.get("salaryRange")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result


class Stripe(Adapter):
    async def run(self):
        response = await self.http.get(self.source.url)
        soup = BeautifulSoup(response.text, "lxml")
        data = json.loads(soup.find("script", id="__NEXT_DATA__").string)["props"]["pageProps"]["jobIndexData"]
        rows = data["listings"]
        self.result.expected_total, self.result.pages = len(rows), 1
        for row in rows:
            filters = data["filters"]
            self.add(self.job(row, title=row["title"], job_id=row["greenhouseId"],
                url=f"https://stripe.com/careers/listing/{row['slug']}/{row['greenhouseId']}",
                locations=[filters["locations"][i] for i in row.get("locationIndices", [])],
                department=[filters["teams"][i] for i in row.get("teamIndices", [])]))
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)

        async def detail(job):
            response = await self.http.get(job.url)
            rows = job_postings(response.text)
            if not rows:
                raise ValueError("Stripe JobPosting missing")
            full = from_schema(self.company.name, "stripe", rows[0], str(response.url))
            full.job_id = job.job_id
            full.raw["listing"] = job.raw
            full.department = job.department
            full.apply_url = job.url.replace("/listing/", "/apply/")
            return full

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result


class Google(Adapter):
    async def run(self):
        page = 1
        base = "https://www.google.com/about/careers/applications/jobs/results/"
        while self.page_guard():
            response = await self.http.get(base, params={"page": page})
            soup = BeautifulSoup(response.text, "lxml")
            script = soup.find("script", class_="ds:1")
            if not script:
                raise ValueError("Google results payload missing")
            data = assignment_json(script.get_text(), "data:")
            rows, total = data[0] or [], data[2]
            self.result.expected_total = int(total)
            before = len(self.seen)
            links = {m.group(1): urljoin("https://www.google.com/about/careers/applications/", a["href"])
                     for a in soup.select("a[href]") if (m := re.search(r"jobs/results/(\d+)", a["href"]))}
            for row in rows:
                if len(row) < 11 or not isinstance(row[0], str):
                    raise ValueError("Google result schema changed")
                def section(index):
                    value = row[index] if len(row) > index else None
                    return value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str) else ""
                description = section(18) + section(4) + section(10) + "<h2>Responsibilities</h2>" + section(3) + section(15)
                self.add(self.job({"embedded_result": row}, title=row[1], job_id=row[0],
                    url=links.get(row[0]) or base + row[0], apply_url=row[2],
                    description=description, method="google.embedded_full_sections", description_url=str(response.url),
                    locations=row[9] or [], qualifications=section(4), responsibilities=section(3),
                    employer=row[7], language=row[8]))
                if self.limit():
                    return self.result
            if len(self.seen) == self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("google_pagination_count_mismatch")
                break
            page += 1
        return self.result


class Apple(Adapter):
    async def run(self):
        page = 1
        base = "https://jobs.apple.com/en-us/search"
        while self.page_guard():
            # An omitted location silently defaults to the visitor's country.
            response = await self.http.get(base, params={"location": "", "page": page})
            state = apple_state(response.text)["search"]
            if state.get("filters", {}).get("locations"):
                raise ValueError("Apple applied an unexpected geographic filter")
            rows = state["searchResults"]
            reported_total = int(state["totalRecords"])
            if self.result.expected_total is None:
                self.result.expected_total = reported_total
            elif reported_total and reported_total != self.result.expected_total:
                self.result.warnings.append("apple_total_changed_during_crawl")
            before = len(self.seen)
            for row in rows:
                self.add(self.job(row, title=row["postingTitle"], job_id=row["id"],
                    requisition_id=row.get("reqId"),
                    url=f"https://jobs.apple.com/en-us/details/{row['id'] if not row['id'].startswith('PIPE-') else row['positionId']}/{row['transformedPostingTitle']}",
                    date_posted=row.get("postDateInGMT"), date_posted_raw=row.get("postingDate"),
                    locations=row.get("locations", []), team=row.get("team")))
                if self.limit():
                    break
            if "sample_limit" in self.result.warnings:
                break
            if len(self.seen) == self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("apple_pagination_count_mismatch")
                break
            page += 1

        async def detail(job):
            response = await self.http.get(job.url)
            schemas = job_postings(response.text)
            if schemas:
                full = from_schema(self.company.name, "apple", schemas[0], str(response.url))
                full.job_id = job.job_id
                full.raw["listing"] = job.raw
                return full
            state = apple_state(response.text)
            job.raw["detail_state"] = state
            info = state["jobDetails"]["jobsData"]
            if not info.get("description"):
                raise ValueError("Apple full description missing")
            fields = ("jobSummary", "description", "minimumQualifications", "keyQualifications",
                      "preferredQualifications", "educationAndExperience", "additionalRequirements",
                      "payAndBenefits", "additionalInformation")
            parts = []
            for field in fields:
                if isinstance(info.get(field), str) and info[field]:
                    value = info[field]
                    parts.append(f"<h2>{field}</h2>" + (value if re.search(r"</?[a-zA-Z][^>]*>", value) else "<p>" + escape(value).replace("\n", "<br>") + "</p>"))
            locale = info.get("selectedLocale", "en_US")
            for footer in info.get("postingFooters", []):
                for block in footer.get("localizations", {}).get(locale, []):
                    parts.append(block.get("content", ""))
            job.description_html = "\n".join(parts)
            job.description_method, job.description_source_url = "apple.jobsData.full_sections", str(response.url)
            job.locations = info.get("locations", job.locations)
            job.qualifications = {key: info.get(key) for key in ("minimumQualifications", "preferredQualifications", "keyQualifications")}
            dates = [v.get("postingDate") for v in info.get("postingPostLocationData", {}).get(locale, {}).values() if v.get("postingDate")]
            if dates:
                job.date_posted = min(dates)
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result
