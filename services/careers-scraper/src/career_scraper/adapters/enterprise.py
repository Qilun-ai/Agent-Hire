from urllib.parse import parse_qs, urlsplit
from urllib.parse import urlencode

from .base import Adapter
from .html import job_postings, from_schema
from .public_ats import epoch
from ..models import text_html


class Oracle(Adapter):
    ROLE_FIELDS = ("ExternalDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr")

    def confirmed_unavailable(self, job):
        # Older saved snapshots have the same checked API evidence but predate the explicit flag.
        row = job.raw.get("detail")
        if (job.description_method == "oracle.external_full_fields" and "source_description_missing" in job.warnings
                and isinstance(row, dict) and str(row.get("Id")) == str(job.job_id)
                and all(key in row for key in self.ROLE_FIELDS)
                and not any(text_html(row[key]) for key in self.ROLE_FIELDS)):
            job.description_unavailable = True
        return super().confirmed_unavailable(job)

    async def run(self):
        p = urlsplit(self.source.url)
        origin = f"{p.scheme}://{p.netloc}"
        site = p.path.split("/sites/")[1].split("/")[0]
        base = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        offset = 0
        while self.page_guard():
            params = {"onlyData": "true", "expand": "requisitionList.workLocation,requisitionList.secondaryLocations",
                      "finder": f"findReqs;siteNumber={site},limit=100,offset={offset},sortBy=POSTING_DATES_DESC",
                      "limit": 100, "offset": offset}
            data = await self.http.json(base, params=params)
            block = data["items"][0]
            self.result.expected_total = int(block["TotalJobsCount"])
            rows = block.get("requisitionList", [])
            before = len(self.seen)
            for row in rows:
                self.add(self.job(row, title=row["Title"], job_id=row["Id"],
                    url=f"{origin}/hcmUI/CandidateExperience/en/sites/{site}/job/{row['Id']}",
                    date_posted=row.get("PostedDate"), locations=[row.get("PrimaryLocation")]
                    + row.get("secondaryLocations", []), workplace_type=row.get("WorkplaceTypeCode")))
                if self.limit():
                    break
            if "sample_limit" in self.result.warnings:
                break
            if len(self.seen) == self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("oracle_pagination_incomplete")
                break
            offset += len(rows)

        async def detail(job):
            api = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            params = {"onlyData": "true", "expand": "all", "finder": f'ById;Id="{job.job_id}",siteNumber={site}'}
            data = await self.http.json(api, params=params)
            rows = data.get("items", [])
            if len(rows) != 1 or str(rows[0].get("Id")) != str(job.job_id):
                raise ValueError("Oracle detail did not return the requested posting")
            row = rows[0]
            job.raw["detail"] = row
            # ShortDescriptionStr is deliberately excluded: it is a listing summary.
            parts = []
            for key in ("ExternalDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr",
                        "OrganizationDescriptionStr", "CorporateDescriptionStr"):
                value = row.get(key)
                if value:
                    parts.append(value)
            has_role = any(text_html(row.get(key)) for key in self.ROLE_FIELDS)
            known_empty = not has_role and all(key in row for key in self.ROLE_FIELDS)
            if known_empty:
                job.description_unavailable = True
                job.warnings.extend(["source_description_missing", "description_not_full_field"])
                self.result.metadata["oracle_confirmed_empty_roles"] = self.result.metadata.get("oracle_confirmed_empty_roles", 0) + 1
            elif not has_role:
                # A changed/omitted API schema is not evidence of an unavailable description.
                response = await self.http.get(job.url)
                schemas = job_postings(response.text)
                if len(schemas) == 1:
                    result = from_schema(self.company.name, "oracle", schemas[0], str(response.url))
                    allowed_ids = {str(job.job_id), str(row.get("RequisitionId"))}
                    if result.job_id is not None and str(result.job_id) not in allowed_ids:
                        raise ValueError("Oracle job-page schema returned a different posting")
                    if result.job_id is None and f"/job/{job.job_id}" not in urlsplit(result.url).path:
                        raise ValueError("Oracle job-page schema did not identify the requested posting")
                    result.job_id = job.job_id
                    result.raw["detail"] = row
                    result.raw["listing"] = job.raw
                    return result
                raise ValueError("Oracle detail omitted its external role fields and no full job-page schema was available")
            job.description_html = "\n".join(parts)
            job.description_source_url, job.description_method = api + "?" + urlencode(params), "oracle.external_full_fields"
            job.title = row.get("Title") or job.title
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else job.requisition_id
            job.date_posted = row.get("ExternalPostedStartDate") or job.date_posted
            job.valid_through = row.get("ExternalPostedEndDate")
            job.qualifications = row.get("ExternalQualificationsStr")
            job.responsibilities = row.get("ExternalResponsibilitiesStr")
            job.employment_type = row.get("JobSchedule") or row.get("JobType")
            job.department = row.get("Department") or row.get("Category")
            job.employer = row.get("LegalEmployer")
            job.education = row.get("StudyLevel")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result


class Eightfold(Adapter):
    async def run(self):
        p = urlsplit(self.source.url)
        origin = f"{p.scheme}://{p.netloc}"
        domain = self.source.options.get("domain") or parse_qs(p.query).get("domain", [None])[0]
        offset = 0
        while self.page_guard():
            params = {"start": offset, "num": 10}
            if domain:
                params["domain"] = domain
            data = await self.http.json(origin + "/api/apply/v2/jobs", params=params)
            self.result.expected_total = int(data["count"])
            domain = domain or data.get("domain")
            rows = data["positions"]
            before = len(self.seen)
            for row in rows:
                self.add(self.job(row, title=row.get("name") or row.get("posting_name"), job_id=row["id"],
                    requisition_id=row.get("display_job_id"), url=row.get("canonicalPositionUrl") or f"{origin}/careers/job/{row['id']}",
                    date_posted=epoch(row.get("t_create"), 1), date_updated=epoch(row.get("t_update"), 1),
                    locations=row.get("locations") or [row.get("location")], department=row.get("department")))
                if self.limit():
                    break
            if "sample_limit" in self.result.warnings:
                break
            if len(self.seen) >= self.result.expected_total:
                self.result.listing_complete = True
                break
            if not rows or len(self.seen) == before:
                self.result.errors.append("eightfold_pagination_incomplete")
                break
            offset += len(rows)

        async def detail(job):
            api = f"{origin}/api/apply/v2/jobs/{job.job_id}"
            row = await self.http.json(api, params={"domain": domain} if domain else {})
            info = row.get("position", row)
            job.raw["detail"] = row
            job.description_html = info.get("job_description") or info.get("description") or ""
            job.description_method, job.description_source_url = "eightfold.job_description", api
            job.apply_url = info.get("apply_url") or info.get("ats_job_url")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result

