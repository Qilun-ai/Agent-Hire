import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..structured import next_flight_values, objects


class Gears42(Adapter):
    """42Gears paginates seven visible cards but embeds its entire opening list in page data."""

    VERIFICATION_REVISION = "42gears.identity-and-final-url.2"

    @staticmethod
    def identity_url(url):
        parsed = urlsplit(url)
        return parsed.hostname, parsed.path.rstrip("/")

    @staticmethod
    def identity_title(title):
        return re.sub(r"\s+", " ", title).strip().casefold()

    async def run(self):
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        response = await self.http.get(self.source.url)
        candidates = []
        for node in objects(next_flight_values(response.text)):
            items = node.get("items")
            if isinstance(items, list) and items and all(
                isinstance(item, dict) and item.get("href", "").startswith("/careers/")
                and "jobTypes" in item and "jobLocations" in item for item in items
            ):
                candidates.append(items)
        if len(candidates) != 1:
            raise ValueError("42Gears full opening list is missing or ambiguous")
        rows = candidates[0]
        self.result.pages, self.result.expected_total = 1, len(rows)
        self.result.metadata["listing_method"] = "next_flight.careers.items"
        for row in rows:
            job = self.job({**row, "company_listing_url": str(response.url)}, title=row["title"],
                           url=urljoin(str(response.url), row["href"]),
                           locations=row["jobLocations"], employment_type=row["jobTypes"])
            if self.identity_url(job.url)[0] != "www.42gears.com" or self.identity_url(job.url)[1] == "/careers":
                raise ValueError("42Gears listing supplied an unexpected detail destination")
            cached = self.options.cached_jobs.get(job.url)
            if cached and (self.identity_url(cached.description_source_url or "") != self.identity_url(job.url)
                           or self.identity_title(cached.title) != self.identity_title(job.title)):
                self.options.cached_jobs.pop(job.url)
            self.add(job)
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)

        async def detail(job):
            response = await self.http.get(job.url)
            job.raw["detail_final_url"] = str(response.url)
            if self.identity_url(str(response.url)) != self.identity_url(job.url):
                raise ValueError("42Gears posting redirected away from its listed job URL")
            soup = BeautifulSoup(response.text, "lxml")
            article, title = soup.select_one("main article"), soup.select_one("main h1")
            if article is None or title is None:
                raise ValueError("42Gears full description article missing")
            if self.identity_title(title.get_text(" ", strip=True)) != self.identity_title(job.title):
                raise ValueError("42Gears detail title does not match the listed posting")
            job.title = title.get_text(" ", strip=True)
            job.description_html = str(article)
            job.description_method, job.description_source_url = "42gears.main.article", str(response.url)
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result
