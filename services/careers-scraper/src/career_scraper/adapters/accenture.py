"""Accenture's own AEM search, inspected 2026-11-06.

The Workday "Access Application" navigation is not a listing source. Market
entry links, request configuration and every job URL come from Accenture itself.
"""
import asyncio
from html import escape
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


class Accenture(Adapter):
    VERIFICATION_REVISION = "accenture.native-market-search.1"
    PAGE_SIZE = 500
    # Above this boundary the public API may return an artificial empty page.
    # Reaching it is explicitly incomplete, even if the next request is empty.
    OFFSET_CEILING = 50000
    API_PATH = "/api/accenture/elastic/findjobs"

    @staticmethod
    def normalized(html):
        return re.sub(r"\s+", "", text_html(html))

    async def page(self, config, offset, size=PAGE_SIZE):
        if not self.page_guard():
            raise ValueError("Accenture pagination request budget exhausted")
        form = {"startIndex": str(offset), "maxResultSize": str(size), "jobKeyword": "",
                "jobCountry": config["data-countrycode"], "jobLanguage": config["data-language-code"],
                "countrySite": config["data-countryselector"], "sortBy": "2", "searchType": "vectorSearch",
                "enableQueryBoost": "true", "minScore": config.get("data-minscore", "0.6"),
                "getFeedbackJudgmentEnabled": "true", "useCleanEmbedding": "true", "score": "true",
                "totalHits": "true", "debugQuery": "false", "jobFilters": "[]"}
        data = await self.http.json(config["api"], method="POST", files={k: (None, v) for k, v in form.items()},
                                    headers={"CSRF-Token": "", "Referer": config["search_url"]})
        total = data.get("totalHits") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or not isinstance(data.get("data"), list) or not isinstance(total, dict)
                or not isinstance(total.get("total"), int) or total["total"] < 0
                or data.get("message", "Success") != "Success"):
            raise ValueError("Accenture native search returned an error or an unrecognized schema")
        if len(data["data"]) > size:
            raise ValueError("Accenture search ignored the requested page size")
        return data["data"], total["total"], str(total.get("overMaxHits", "false")).lower() == "true"

    @staticmethod
    def job_url(row, config):
        value = row["jobDetailUrl"].replace("{0}", config["data-countryselector"])
        url = urljoin(config["search_url"], value)
        parsed = urlsplit(url)
        if (parsed.hostname != urlsplit(config["search_url"]).hostname
                or not parsed.path.endswith("/careers/jobdetails")
                or parse_qs(parsed.query).get("id") != [row["guid"]]):
            raise ValueError("Accenture listing ID and native detail URL do not match")
        return url

    @staticmethod
    def qualification(row):
        value, additional = row.get("qualification") or "", row.get("additionalInformation") or ""
        # The API appends the exact legal-information field to qualification.
        # Keep it once, in the additional-information section, without truncating qualifications.
        return value[:-len(additional)].strip() if additional and value.endswith(additional) else value

    async def template(self, row, config):
        url = self.job_url(row, config)
        response = await self.http.get(url)
        if parse_qs(urlsplit(str(response.url)).query).get("id") != [row["guid"]]:
            raise ValueError("Accenture reference job redirected away from its exact posting ID")
        soup = BeautifulSoup(response.text, "lxml")
        title = soup.select_one("h1.rad-job-details-hero__title")
        main = soup.select_one('[id$="-job-description-content"] .rad-accordion-atom__content')
        if (title is None or title.get_text(" ", strip=True) != row["title"] or main is None
                or self.normalized(str(main)) != self.normalized(row.get("jobDescription"))):
            raise ValueError("Accenture full API description does not match its native detail page")
        about = soup.select_one('[id$="-about-accenture-content"] .rad-accordion-atom__content')
        return {"about_html": about.decode_contents() if about else "", "source_url": str(response.url),
                "reference_guid": row["guid"], "main_description_matched": True}

    def collect(self, row, config, template):
        url = self.job_url(row, config)
        guid = row["guid"]
        if guid in self.posting_ids:
            # The same posting may be exposed by more than one market navigation.
            self.posting_ids[guid].raw.setdefault("observed_market_sites", []).append(config["data-countryselector"])
            return
        sections = [("Job Description", row.get("jobDescription") or ""),
                    ("Qualification", self.qualification(row)),
                    ("Additional Information", row.get("additionalInformation") or ""),
                    ("About Accenture", template["about_html"])]
        description = "\n".join(f"<section><h2>{escape(title)}</h2>{body}</section>" for title, body in sections if body)
        job = self.job({**row, "company_listing_url": config["search_url"],
                        "observed_market_sites": [config["data-countryselector"]],
                        "template_reference": template["source_url"]},
                       title=row["title"], job_id=guid, requisition_id=row.get("requisitionId"), url=url,
                       description=description, method="accenture.native_search.full_fields_and_market_about",
                       description_url=config["api"], locations=row.get("location") or [],
                       qualifications=self.qualification(row), education=row.get("education"),
                       department=row.get("jobFamilyGroup"), employment_type=row.get("jobScheduleDescription"),
                       workplace_type=row.get("remoteType"), experience=row.get("yearsOfExperience"),
                       date_posted_raw=row.get("postedDateText"), date_updated=row.get("updateDate"),
                       language=config["data-language-code"], employer="Accenture")
        # Some Chilean postings put their entire role in the Qualifications tab
        # and publish only a full stop in Job Description (confirmed in browser).
        # Corporate/legal template text cannot make an empty role pass this check.
        role_text = text_html((row.get("jobDescription") or "") + self.qualification(row))
        if len(role_text.strip()) < 100:
            job.warnings.append("description_not_full_field")
        self.posting_ids[guid] = job
        self.add(job)

    async def market(self, config):
        rows, advertised, capped = await self.page(config, 0)
        report = {"site": config["data-countryselector"], "search_url": config["search_url"],
                  "advertised_total": advertised, "advertised_total_capped": capped,
                  "jobs": 0, "complete": False}
        self.result.metadata["markets"].append(report)
        if not rows:
            if advertised:
                raise ValueError("Accenture returned an empty first page with a positive total")
            report["complete"] = True
            return
        template = await self.template(rows[0], config)
        self.result.metadata["native_detail_references"].append(template)
        first_ids = [r["guid"] for r in rows[:12]]
        market_ids = set()

        def accept(page):
            for row in page:
                if row["guid"] in market_ids:
                    raise ValueError("Accenture repeated a posting across pages within one market")
                market_ids.add(row["guid"])
                self.collect(row, config, template)
                report["jobs"] = len(market_ids)
                if self.limit():
                    return False
            return True

        if not accept(rows):
            return
        offset = self.PAGE_SIZE
        ended = len(rows) < self.PAGE_SIZE
        while not ended and (capped or offset < advertised):
            if offset >= self.OFFSET_CEILING:
                raise ValueError("Accenture native API ceiling reached; global completeness is unproven")
            offsets = list(range(offset, min(offset + 4 * self.PAGE_SIZE,
                                            self.OFFSET_CEILING, advertised if not capped else self.OFFSET_CEILING), self.PAGE_SIZE))
            pages = await asyncio.gather(*(self.page(config, start) for start in offsets), return_exceptions=True)
            for page in pages:
                if isinstance(page, BaseException):
                    raise page
            for page, _, _ in pages:
                if ended:
                    if page:
                        raise ValueError("Accenture returned jobs after an earlier empty/short page")
                    continue
                if not accept(page):
                    return
                ended = len(page) < self.PAGE_SIZE
            offset += len(offsets) * self.PAGE_SIZE
        if not capped and len(market_ids) != advertised:
            raise ValueError(f"Accenture market count mismatch: {len(market_ids)} != {advertised}")
        if capped and len(market_ids) >= self.OFFSET_CEILING:
            raise ValueError("Accenture native API ceiling reached")
        final, final_total, final_capped = await self.page(config, 0, len(first_ids))
        if [r["guid"] for r in final] != first_ids or (advertised, capped) != (final_total, final_capped):
            raise ValueError("Accenture native listing changed during collection")
        report["complete"] = True

    async def run(self):
        self.posting_ids = {}
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,
                                    listing_method="accenture.native_market_pages_and_their_search_requests",
                                    markets=[], native_detail_references=[])
        previous = self.options.previous_report
        old_metadata = previous.get("metadata", {})
        resumable = old_metadata.get("verification_revision") == self.VERIFICATION_REVISION
        previous_markets = {m["search_url"].split("/")[3]: m for m in old_metadata.get("markets", []) if m["complete"]} if resumable else {}
        cached = {job.key: job for job in self.options.cached_jobs.values()}
        if resumable:
            self.result.metadata["native_detail_references"] = list(old_metadata.get("native_detail_references", []))
            self.result.metadata["previous_attempt_duration_seconds"] = previous.get("duration_seconds")
            self.result.metadata["previous_attempt_errors"] = previous.get("errors", [])
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text, "lxml")
        market_urls = sorted({urljoin(str(entry.url), a["href"]) for a in soup.select("a[href]")
                              if re.fullmatch(r"/[a-z]{2}-[a-z]{2}/careers/?", urlsplit(a["href"]).path)
                              and urlsplit(urljoin(str(entry.url), a["href"])).hostname == urlsplit(str(entry.url)).hostname})
        if not market_urls:
            raise ValueError("Accenture official country/language career navigation is missing")
        self.result.metadata["official_market_entry_urls"] = market_urls
        for market_url in market_urls:
            try:
                site = urlsplit(market_url).path.strip("/").split("/")[0]
                saved = previous_markets.get(site)
                market_jobs = [job for job in cached.values() if site in job.raw.get("observed_market_sites", [])]
                if saved and len({job.job_id for job in market_jobs}) == saved["jobs"]:
                    for job in market_jobs:
                        if job.job_id not in self.posting_ids:
                            self.posting_ids[job.job_id] = job
                            self.seen.add(job.key)
                            self.result.jobs.append(job)
                    self.result.metadata["markets"].append({**saved, "reused_from_completed_snapshot": True})
                    continue
                home = await self.http.get(market_url)
                home_soup = BeautifulSoup(home.text, "lxml")
                search_urls = {urljoin(str(home.url), a["href"]) for a in home_soup.select("a[href]")
                               if urlsplit(a["href"]).path.endswith("/careers/jobsearch")}
                same_market = {u for u in search_urls if urlsplit(u).path.strip("/").split("/")[0] == site}
                if len(same_market) == 1:
                    search_urls = same_market
                if len(search_urls) != 1:
                    raise ValueError("Accenture market has no unambiguous company job-search link")
                response = await self.http.get(search_urls.pop())
                component = BeautifulSoup(response.text, "lxml").select_one(".rad-job-search__filters-and-cards")
                if component is None:
                    raise ValueError("Accenture market uses a different job-search implementation")
                config = {**component.attrs, "search_url": str(response.url), "api": urljoin(str(response.url), self.API_PATH)}
                await self.market(config)
                if "sample_limit" in self.result.warnings:
                    return self.result
            except Exception as exc:
                self.result.errors.append(f"Market {market_url}: {type(exc).__name__}: {exc}")
        self.result.listing_complete = (not self.result.errors and len(self.result.metadata["markets"]) == len(market_urls)
                                        and all(m["complete"] for m in self.result.metadata["markets"]))
        missing = [job for job in self.result.jobs if not job.description_verified]
        if missing:
            from .accenture_details import recover_exact
            repaired = await self.hydrate(missing, lambda job: recover_exact(self.http, job))
            by_identity = {job.key: job for job in repaired}
            self.result.jobs = [by_identity.get(job.key, job) for job in self.result.jobs]
        return self.result
