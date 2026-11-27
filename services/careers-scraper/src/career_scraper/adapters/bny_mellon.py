"""BNY Mellon's own official careers journey (Oracle Candidate Experience CX_3001).

The requested entry (www.bny.com/corporate/global/en/careers.html) redirects to
the official Work With Us page, whose "Search jobs" action links to the hosted
`BNY-Careers` board. That board's published `<base>` tag identifies the exact
site number (`CX_3001`) and the public siteSettings name the site as BNY. The
adapter requests only the unfiltered `findReqs` listing from that exact site,
paginates it completely, and hydrates full details for each exact listed ID.
The contractor TalentNet community linked from the same page is a separate
talent-pool surface with no exposed vacancy IDs and is deliberately excluded.
Corporate boilerplate fields (`CorporateDescriptionStr`,
`OrganizationDescriptionStr`) are retained in raw but never used as the
normalized role description.
"""
import gzip
from hashlib import sha256
from html import escape
import json
from pathlib import PurePosixPath
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..http import HTTP
from ..models import now, text_html


ROLE_FIELDS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
# Corporate/organization sections are independent boilerplate, not role bodies.
BOILERPLATE_FIELDS = ("CorporateDescriptionStr", "OrganizationDescriptionStr")
NONPUBLIC_FIELDS = frozenset({"InternalQualificationsStr", "InternalResponsibilitiesStr",
                              "HiringManager", "ObjectVerNumberProfile", "ApplyWhenNotPostedFlag"})
EXPAND = ("requisitionList.workLocation,requisitionList.secondaryLocations,"
          "flexFieldsFacet.values,requisitionList.requisitionFlexFields")
FACETS = "WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;POSTING_DATES;FLEX_FIELDS;LOCATIONS"
FILTER_FIELDS = ("Keyword", "Location", "LocationId", "SelectedTitlesFacet", "SelectedCategoriesFacet",
                 "SelectedPostingDatesFacet", "SelectedLocationsFacet", "SelectedWorkLocationsFacet",
                 "SelectedFlexFieldsFacets", "SelectedOrganizationsFacet", "SelectedWorkplaceTypesFacet",
                 "JobFamilyId", "PostingStartDate", "PostingEndDate", "RequisitionId", "CandidateNumber",
                 "OrganizationId", "WorkplaceType")


def public_detail(row):
    """Retain source job metadata while omitting internal/personnel fields."""
    return {key: value for key, value in row.items()
            if key not in NONPUBLIC_FIELDS and not key.startswith("Internal")}


def full_body(row):
    """Join the independent external role sections only; boilerplate excluded."""
    return "\n".join(row[key] for key in ROLE_FIELDS if row.get(key))


def salary_fields(row):
    values = [{"label": f["Prompt"], "value": f.get("Value")}
              for f in row.get("requisitionFlexFields", [])
              if any(word in f.get("Prompt", "").casefold() for word in ("salary", "compensation", "pay range"))]
    return values or None


class BNYMellon(Adapter):
    VERIFICATION_REVISION = "bny_mellon.official-BNY-Careers-CX_3001-exact-details.1"
    API_ORIGIN = "https://eofe.fa.us2.oraclecloud.com"
    SITE = "CX_3001"
    SHORT_URL = "BNY-Careers"
    PAGE_SIZE = 100

    def evidence(self, response, value):
        """Archive exact public job fields, excluding unrelated internal fields."""
        if not self.http.output:
            return
        encoded = json.dumps(value, ensure_ascii=False).encode()
        digest = sha256(encoded).hexdigest()
        path = self.http.output / "responses" / f"{digest}.gz"
        if not path.exists():
            path.write_bytes(gzip.compress(encoded))
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps({"url": str(response.request.url), "final_url": str(response.url),
                                     "method": "GET", "request_body": None, "status": response.status_code,
                                     "sha256": digest, "fetched_at": now(),
                                     "evidence_scope": "Original public job fields; internal qualifications/responsibilities, hiring manager and admin flags omitted"}) + "\n")

    def _expected_job_url(self, identity):
        return f"{self.API_ORIGIN}/hcmUI/CandidateExperience/en/sites/{self.SHORT_URL}/job/{identity}"

    async def listing(self, api, offset):
        if not self.page_guard():
            raise ValueError("BNY Mellon native pagination budget exhausted")
        params = {"onlyData": "true", "expand": EXPAND,
                  "finder": f"findReqs;siteNumber={self.SITE},facetsList={FACETS},limit={self.PAGE_SIZE},sortBy=POSTING_DATES_DESC,offset={offset}"}
        response = await api.get(self.api + "/recruitingCEJobRequisitions", params=params,
                                 headers={"Ora-Irc-Language": "en", "Accept-Language": "en"})
        data = response.json()
        blocks = data.get("items")
        if not isinstance(blocks, list) or len(blocks) != 1:
            raise ValueError("BNY Mellon listing response has no single public site result")
        block = blocks[0]
        rows, total = block.get("requisitionList"), block.get("TotalJobsCount")
        if (not isinstance(rows, list) or not isinstance(total, int) or total < 0
                or block.get("SiteNumber") != self.SITE or block.get("Offset") != offset
                or block.get("Limit") != self.PAGE_SIZE or block.get("SortBy") != "POSTING_DATES_DESC" or len(rows) > self.PAGE_SIZE
                or any(block.get(key) not in (None, "", False) for key in FILTER_FIELDS)):
            raise ValueError("BNY Mellon listing identity, pagination or unfiltered search state changed")
        self.evidence(response, data)
        return rows, total

    async def detail(self, api, job):
        params = {"expand": "all", "onlyData": "true", "finder": f'ById;Id="{job.job_id}",siteNumber={self.SITE}'}
        response = await api.get(self.api + "/recruitingCEJobRequisitionDetails", params=params,
                                 headers={"Ora-Irc-Language": "en", "Accept-Language": "en"})
        data = response.json()
        rows = data.get("items", [])
        if (len(rows) != 1 or str(rows[0].get("Id")) != job.job_id or rows[0].get("Title") != job.title):
            raise ValueError("BNY Mellon exact detail does not match the company-published posting")
        row = public_detail(rows[0])
        self.evidence(response, {**data, "items": [row]})
        job.raw["detail"] = row
        job.raw["native_detail_url"] = job.url
        # Corporate/organization boilerplate cannot replace missing role content.
        if len("\n".join(text_html(row.get(key)).strip() for key in ROLE_FIELDS)) < 100:
            # The exact published posting carries no external role sections; only
            # corporate boilerplate templates. Retain the record honestly as a
            # source-empty posting instead of inventing a description.
            job.description_html = ""
            job.description_text = ""
            job.description_method = "bny_mellon.official_BNY-Careers.exact_external_sections"
            job.description_source_url = str(response.url)
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") is not None else None
            job.apply_url = job.url + "/apply"
            job.date_posted = row.get("ExternalPostedStartDate") or None
            job.date_posted_raw = {"listing_PostedDate": job.raw["listing"].get("PostedDate"),
                                   "ExternalPostedStartDate": row.get("ExternalPostedStartDate")}
            job.valid_through = row.get("ExternalPostedEndDate") or None
            job.language = row.get("ContentLocale") or None
            job.locations = [{"PrimaryLocation": row.get("PrimaryLocation"),
                              "PrimaryLocationCountry": row.get("PrimaryLocationCountry"),
                              "primaryLocationCoordinates": row.get("primaryLocationCoordinates", [])},
                             *row.get("secondaryLocations", []), *row.get("workLocation", []),
                             *row.get("otherWorkLocations", [])]
            job.department = row.get("Department") or row.get("Category") or None
            job.team = row.get("JobFunction") or None
            job.employment_type = row.get("JobSchedule") or row.get("JobType") or None
            job.workplace_type = row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or None
            job.salary = salary_fields(row)
            job.qualifications = row.get("ExternalQualificationsStr")
            job.responsibilities = row.get("ExternalResponsibilitiesStr")
            job.education = row.get("StudyLevel") or None
            job.experience = row.get("JobLevel") or None
            job.employer = row.get("LegalEmployer") or None
            job.description_unavailable = True
            job.warnings.extend(["source_description_missing", "description_not_full_field"])
            return job
        if any(key not in row for key in ROLE_FIELDS):
            raise ValueError("BNY Mellon exact detail omitted an inspected full description field")
        job.description_html = full_body(row)
        if len(text_html(job.description_html)) < 100:
            raise ValueError("BNY Mellon exact posting's complete role fields require review")
        job.description_method = "bny_mellon.official_BNY-Careers.exact_external_sections"
        job.description_source_url = str(response.url)
        job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") is not None else None
        job.apply_url = job.url + "/apply"
        job.date_posted = row.get("ExternalPostedStartDate") or None
        job.date_posted_raw = {"listing_PostedDate": job.raw["listing"].get("PostedDate"),
                               "ExternalPostedStartDate": row.get("ExternalPostedStartDate")}
        job.valid_through = row.get("ExternalPostedEndDate") or None
        job.language = row.get("ContentLocale") or None
        job.locations = [{"PrimaryLocation": row.get("PrimaryLocation"),
                          "PrimaryLocationCountry": row.get("PrimaryLocationCountry"),
                          "primaryLocationCoordinates": row.get("primaryLocationCoordinates", [])},
                         *row.get("secondaryLocations", []), *row.get("workLocation", []),
                         *row.get("otherWorkLocations", [])]
        job.department = row.get("Department") or row.get("Category") or None
        job.team = row.get("JobFunction") or None
        job.employment_type = row.get("JobSchedule") or row.get("JobType") or None
        job.workplace_type = row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or None
        job.salary = salary_fields(row)
        job.qualifications = row.get("ExternalQualificationsStr")
        job.responsibilities = row.get("ExternalResponsibilitiesStr")
        job.education = row.get("StudyLevel") or None
        job.experience = row.get("JobLevel") or None
        job.employer = row.get("LegalEmployer") or None
        attachments = []
        # Published role documents are part of the role; corporate navigation links
        # are retained as HTML, not mistaken for a job file.
        for key in ROLE_FIELDS:
            soup = BeautifulSoup(row.get(key) or "", "lxml")
            for anchor in soup.select("a[href]"):
                url = urljoin(job.url, anchor["href"])
                suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
                if suffix in {".pdf", ".doc", ".docx"} and url not in {a["url"] for a in attachments}:
                    document = await self.http.get(url)
                    extracted = extract_attachment(document.content, PurePosixPath(urlsplit(url).path).name)
                    attachments.append({"url": url, "source_field": key, **extracted})
                    job.description_html += "\n<pre>" + escape(extracted["text"]) + "</pre>"
        job.raw["attachments"] = attachments
        return job

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        entry_parts = urlsplit(str(entry.url))
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"www.bny.com", "bny.com"}
                or entry_parts.path != "/corporate/global/en/about-us/careers/work-with-us.html"):
            raise ValueError("BNY Mellon dedicated flow must start from its official HTTPS careers entry")
        soup = BeautifulSoup(entry.text, "lxml")
        links = {urljoin(str(entry.url), a["href"]) for a in soup.select("a[href]")
                 if a.get_text(" ", strip=True) == "Search jobs"
                 and urlsplit(urljoin(str(entry.url), a["href"])).hostname == "eofe.fa.us2.oraclecloud.com"
                 and urlsplit(a["href"]).path == f"/hcmUI/CandidateExperience/en/sites/{self.SHORT_URL}/jobs"}
        if len(links) != 1:
            raise ValueError("BNY Mellon official Search jobs destination is absent or ambiguous")
        portal = await self.http.get(links.pop())
        base = BeautifulSoup(portal.text, "lxml").select_one("base[data-apibaseurl][data-sitenumber]")
        if (urlsplit(str(portal.url)).hostname != "eofe.fa.us2.oraclecloud.com" or base is None
                or base.get("data-sitenumber") != self.SITE
                or urlsplit(base.get("data-apibaseurl", "")).hostname != "eofe.fa.us2.oraclecloud.com"
                or base.get("href") != f"/hcmUI/CandidateExperience/en/sites/{self.SHORT_URL}"):
            raise ValueError("BNY Mellon officially linked portal/site/API identity changed")
        self.api = self.API_ORIGIN + "/hcmRestApi/resources/latest"
        settings = await self.http.json(self.API_ORIGIN + f"/hcmRestApi/CandidateExperience/en/siteSettings/{self.SITE}")
        config = settings.get("app", {})
        if (config.get("siteName") != "BNY" or config.get("siteNumber") != self.SITE
                or config.get("siteCode") != "BNY_EXTERNAL_CAREER_SITE"):
            raise ValueError("BNY Mellon public siteSettings no longer identify the official company site")
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION, official_entry=str(entry.url),
                                    company_listing_url=str(portal.url), site_number=self.SITE,
                                    listing_authority="BNY Mellon official Search jobs global unfiltered BNY-Careers portal (CX_3001)",
                                    contractor_community_excluded="https://us.bnymellon.talentnet.community (TalentNet tenant us.bny): separate contractor talent-pool surface with no exposed vacancy IDs; no board enumerated",
                                    native_page_size=self.PAGE_SIZE, public_site_config={key: config.get(key) for key in
                                    ("siteName", "siteCode", "siteNumber", "siteLang", "languages", "templates")})
        async with HTTP(None, delay=self.http.delay, timeout=self.http.client.timeout, retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            started = time.perf_counter()
            ids, initial_pages = set(), []
            offset = 0
            while True:
                rows, total = await self.listing(api, offset)
                if self.result.expected_total is None:
                    self.result.expected_total = total
                elif total != self.result.expected_total:
                    raise ValueError("BNY Mellon listing total changed during pagination")
                page_ids = []
                for row in rows:
                    identity = row.get("Id")
                    if not isinstance(identity, str) or not identity or not row.get("Title"):
                        raise ValueError("BNY Mellon public posting identity missing")
                    if identity in ids:
                        # Oracle's offset pagination can overlap at boundaries; record
                        # the overlap once and let page reconciliation judge membership.
                        self.result.warnings.append(f"duplicate_listing_id:{identity}")
                        continue
                    ids.add(identity)
                    page_ids.append(identity)
                    self.add(self.job({"listing": row}, title=row["Title"], job_id=identity,
                                      url=self._expected_job_url(identity)))
                    if self.limit():
                        break
                initial_pages.append({"offset": offset, "ids": page_ids})
                if "sample_limit" in self.result.warnings or len(ids) == self.result.expected_total:
                    break
                if len(ids) > self.result.expected_total:
                    raise ValueError("BNY Mellon listing exceeded its published global total")
                if len(rows) < self.PAGE_SIZE:
                    # Short final page. If the unique count still falls short of the
                    # published total, listing_complete stays false and the run is
                    # reported as partial with the exact reconciliation evidence.
                    break
                offset += self.PAGE_SIZE
            self.result.timings["initial_listing_seconds"] = time.perf_counter() - started
            self.result.listing_complete = len(ids) == self.result.expected_total and "sample_limit" not in self.result.warnings
            self.result.metadata["initial_listing_pages"] = initial_pages
            self.result.jobs = await self.hydrate(self.result.jobs, lambda job: self.detail(api, job))
            if "sample_limit" not in self.result.warnings:
                final_started = time.perf_counter()
                final, final_pages, offset = set(), [], 0
                while True:
                    rows, total = await self.listing(api, offset)
                    current = [row["Id"] for row in rows]
                    if (total != self.result.expected_total or len(set(current)) != len(current)
                            or final.intersection(current) or (not current and total)):
                        self.result.errors.append("bny_mellon_final_listing_did_not_reconcile")
                        break
                    final.update(current)
                    final_pages.append({"offset": offset, "ids": current})
                    if len(final) >= total or len(rows) < self.PAGE_SIZE:
                        break
                    offset += self.PAGE_SIZE
                if final and final != ids:
                    self.result.errors.append("bny_mellon_listing_id_set_changed")
                self.result.listing_complete = self.result.listing_complete and (not final or final == ids) and not self.result.errors
                self.result.metadata["final_listing_pages"] = final_pages
                self.result.metadata["final_listing_ids"] = sorted(final) if final else None
                self.result.metadata["final_listing_reconciled"] = bool(final) and final == ids
                self.result.timings["final_listing_seconds"] = time.perf_counter() - final_started
            self.result.metadata["company_published_job_ids"] = sorted(ids)
        return self.result
