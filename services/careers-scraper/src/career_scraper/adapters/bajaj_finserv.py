"""Bajaj Finserv / Bajaj Finance Limited careers via its published PeopleStrong portal."""

from __future__ import annotations

import time

import re

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.bajajfinserv.in/careers-finance"
PORTAL_ORIGIN = "https://bflcareers.peoplestrong.com"
LISTING_API = PORTAL_ORIGIN + "/api/cp/rest/altone/cp/jobs/v1"
DETAIL_API = PORTAL_ORIGIN + "/api/cp/rest/altone/cp/job/{job_code}/v2"
DETAIL_PARTS = "basic,organisational,descriprion,workflow,skill,qualification,certification,language,applied"
PAGE_SIZE = 99
EMPLOYER_NAME = "Bajaj Finance Limited"


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def normalize_date(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return None




def _extract_section(text, heading, following_headings):
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = None
    for index, line in enumerate(lines):
        if line.lower() == heading.lower():
            start = index + 1
            break
    if start is None:
        return None
    collected = []
    for line in lines[start:]:
        if line.lower() in {h.lower() for h in following_headings}:
            break
        collected.append(line)
    value = " ".join(collected).strip()
    return value or None


def _location_parts(location_complete):
    # "India>JHARKHAND>North>Godda>Godda>Tier 4"
    if not location_complete:
        return {}
    parts = [part.strip() for part in str(location_complete).split(">")]
    result = {}
    labels = ["country", "state", "region", "city", "location", "tier"]
    for index, label in enumerate(labels):
        if index < len(parts) and parts[index]:
            result[label] = parts[index]
    return result


def parse_listing_page(value, *, requested_offset=0, requested_limit=PAGE_SIZE):
    if not isinstance(value, dict):
        raise ValueError("Bajaj Finserv PeopleStrong listing response is not an object")
    total = value.get("totalRecords")
    rows_raw = value.get("response")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Bajaj Finserv PeopleStrong listing response has an invalid totalRecords value")
    if not isinstance(rows_raw, list):
        raise ValueError("Bajaj Finserv PeopleStrong listing response has an invalid response list")

    issues = []
    rows = []
    seen = set()
    for row in rows_raw:
        if not isinstance(row, dict):
            issues.append("listing_row_not_object")
            continue
        code = _clean(row.get("jobCode"))
        if not code or not re.fullmatch(r"JR\d+", code):
            issues.append("listing_row_missing_job_code")
            continue
        if code in seen:
            issues.append(f"listing_duplicate_job_code:{code}")
            continue
        if not _clean(row.get("jobTitle")) or not _clean(row.get("jobDetailUrl")):
            issues.append(f"listing_row_missing_title_or_url:{code}")
            continue
        expected_limit = requested_limit
        if len(rows_raw) > expected_limit:
            issues.append("listing_page_exceeds_requested_limit")
        seen.add(code)
        rows.append({
            "jobCode": code,
            "jobTitle": _clean(row.get("jobTitle")),
            "jobDetailUrl": _clean(row.get("jobDetailUrl")),
            "organizationUnit": _clean(row.get("organizationUnit")),
            "organizationUnitComplete": _clean(row.get("organizationUnitComplete")),
            "locationHierarchy": _clean(row.get("locationHierarchy")),
            "locationHierarchyComplete": _clean(row.get("locationHierarchyComplete")),
            "jobPostedDate": normalize_date(row.get("jobPostedDate")),
            "jobPostedDateRaw": row.get("jobPostedDate"),
            "jobClosureDate": normalize_date(row.get("jobClosureDate")),
            "jobClosureDateRaw": row.get("jobClosureDate"),
            "expRange": _clean(row.get("expRange")),
            "skills": row.get("skills"),
            "openings": row.get("openings"),
            "requisitionId": row.get("requisitionId"),
            "designation": _clean(row.get("designation")),
            "designationID": row.get("designationID"),
            "worksiteID": row.get("worksiteID"),
            "hrOrgUnitID": row.get("hrOrgUnitID"),
            "functionalArea": row.get("functionalArea"),
            "raw": row,
        })

    if requested_offset + len(rows_raw) > total and len(rows_raw):
        # Allow the final partial page; otherwise flag a count mismatch.
        if requested_offset + len(rows_raw) != total and requested_offset != 0:
            pass
    expected_page_rows = min(requested_limit, max(0, total - requested_offset))
    if len(rows_raw) != expected_page_rows:
        issues.append("listing_page_row_count_mismatch")

    return {
        "rows": rows,
        "total": total,
        "offset": requested_offset,
        "limit": requested_limit,
        "returned": len(rows_raw),
        "issues": issues,
    }


def parse_detail(value, *, job_code=None):
    if not isinstance(value, dict):
        raise ValueError("Bajaj Finserv PeopleStrong detail response is not an object")
    payload = value.get("response")
    if not isinstance(payload, dict):
        raise ValueError("Bajaj Finserv PeopleStrong detail response is missing its response object")
    message = value.get("messageCode")
    if isinstance(message, dict) and message.get("code") not in (None, 200):
        raise ValueError(f"Bajaj Finserv PeopleStrong detail API returned code {message.get('code')}")

    code = _clean(payload.get("requisitionCode")) or job_code
    if not code:
        raise ValueError("Bajaj Finserv PeopleStrong detail response is missing requisitionCode")
    title = _clean(payload.get("jobTitle"))
    if not title:
        raise ValueError(f"Bajaj Finserv PeopleStrong detail response for {code} is missing jobTitle")

    description_html = payload.get("jobDescription")
    if not isinstance(description_html, str) or not description_html.strip():
        raise ValueError(f"Bajaj Finserv PeopleStrong detail response for {code} is missing jobDescription")

    location_complete = _clean(payload.get("locationHierarchyComplete"))
    org_complete = _clean(payload.get("organizationUnitComplete"))
    location_parts = _location_parts(location_complete)
    city = _clean(payload.get("joiningLocation")) or location_parts.get("city")
    locations = []
    if city:
        locations.append(city)
    if location_complete and location_complete not in locations:
        locations.append(location_complete)

    min_salary = payload.get("minSalary")
    max_salary = payload.get("maxSalary")
    currency = _clean(payload.get("currency"))
    salary = None
    try:
        min_value = float(min_salary) if min_salary is not None else 0
        max_value = float(max_salary) if max_salary is not None else 0
    except (TypeError, ValueError):
        min_value = 0
        max_value = 0
    if min_value > 0 or max_value > 0:
        salary = {
            "min": min_salary,
            "max": max_salary,
            "currency": currency,
            "min_budget": payload.get("minBudgetSalary"),
            "max_budget": payload.get("maxBudgetSalary"),
        }

    qualifications = payload.get("qualifications")
    if not isinstance(qualifications, list):
        qualifications = [qualifications] if qualifications else []
    qualifications = [_clean(q) for q in qualifications if _clean(q)]

    skills_raw = payload.get("skills")
    skills = []
    if isinstance(skills_raw, list):
        for skill in skills_raw:
            if isinstance(skill, dict):
                text = _clean(skill.get("text"))
                if text:
                    skills.append({"skill": text, "mandatory": skill.get("mandatory")})
            else:
                text = _clean(skill)
                if text:
                    skills.append(text)

    certifications = payload.get("certifications")
    if not isinstance(certifications, list):
        certifications = [certifications] if certifications else []
    languages = payload.get("languages")
    if not isinstance(languages, list):
        languages = [languages] if languages else []

    employment_type = _clean(payload.get("employmentType"))
    employment_tenure = _clean(payload.get("employmentTenureType"))
    experience = None
    if payload.get("minimumExp") is not None or payload.get("maximumExp") is not None:
        experience = {
            "min_years": payload.get("minimumExp"),
            "max_years": payload.get("maximumExp"),
            "range": None,
        }
    # Prefer the listing's human-readable range when available; filled by caller if needed.

    posted = normalize_date(payload.get("CandidatePortalStartDate"))
    valid_through = normalize_date(payload.get("CandidatePortalEndDate"))

    description_text = text_html(description_html)
    responsibilities = _extract_section(
        description_text,
        "Duties and Responsibilities",
        ["Required Qualifications and Experience", "Required Qualifications", "Skills"],
    )
    qualification_text = _extract_section(
        description_text,
        "Required Qualifications and Experience",
        ["Duties and Responsibilities", "Job Purpose"],
    )
    if not qualifications and qualification_text:
        qualifications = [qualification_text]
    elif qualification_text and qualifications == ["Others"]:
        qualifications = [qualification_text]

    return {
        "job_code": code,
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "locations": locations,
        "location_complete": location_complete,
        "location_parts": location_parts,
        "city": city,
        "department": _clean(payload.get("orgUnitCode")) or (_clean(org_complete.split(">")[1]) if org_complete and ">" in org_complete else None),
        "organization_complete": org_complete,
        "employer": EMPLOYER_NAME,
        "employment_type": employment_type or employment_tenure,
        "experience": experience,
        "salary": salary,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "skills": skills,
        "certifications": certifications,
        "languages": languages,
        "posted": posted,
        "valid_through": valid_through,
        "openings": payload.get("noOfOpening"),
        "band": _clean(payload.get("band")),
        "grade": _clean(payload.get("gradeCode")),
        "requisition_id": _clean(payload.get("requisitionId")),
        "requisition_type": _clean(payload.get("requisitionType")),
        "requisition_status": _clean(payload.get("requisitionStatus")),
        "hiring_manager": _clean(payload.get("hiringManagerName")),
        "workflow": _clean(payload.get("WorkflowName")),
        "min_salary": payload.get("minSalary"),
        "max_salary": payload.get("maxSalary"),
        "raw": payload,
    }


class BajajFinserv(Adapter):
    VERIFICATION_REVISION = "bajaj-finserv.peoplestrong-portal.1"

    async def run(self):
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_API,
            "portal_url": PORTAL_ORIGIN + "/job/joblist",
            "membership_authority": "Bajaj Finance Limited PeopleStrong candidate portal linked from the official Bajaj Finserv careers page",
            "adapter_made_network_requests": True,
        })
        self.result.expected_total = None
        self.result.listing_complete = False

        seen_codes: set[str] = set()
        offset = 0
        total = None

        while True:
            if total is not None and offset >= total:
                break
            if not self.page_guard():
                break
            page_started = time.perf_counter()
            data = await self.http.json(
                LISTING_API,
                method="POST",
                params={"offset": offset, "limit": PAGE_SIZE},
                json={},
            )
            page_seconds = time.perf_counter() - page_started
            self.result.timings["source_collection_seconds"] = self.result.timings.get("source_collection_seconds", 0.0) + page_seconds

            parsed = parse_listing_page(data, requested_offset=offset, requested_limit=PAGE_SIZE)
            if total is None:
                total = parsed["total"]
                self.result.expected_total = total
            elif parsed["total"] != total:
                self.result.warnings.append("listing_total_changed_between_pages")
            if parsed["issues"]:
                self.result.warnings.extend(parsed["issues"])

            page_rows = 0
            for row in parsed["rows"]:
                code = row["jobCode"]
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                job = self.job(
                    raw=row["raw"],
                    title=row["jobTitle"],
                    url=row["jobDetailUrl"],
                    job_id=code,
                    description="",
                    method=None,
                    description_url=row["jobDetailUrl"],
                    requisition_id=str(row["requisitionId"]) if row["requisitionId"] is not None else None,
                    locations=[row["locationHierarchy"]] if row["locationHierarchy"] else [],
                    department=row["organizationUnit"],
                    team=row["organizationUnitComplete"],
                    employment_type=None,
                    date_posted=row["jobPostedDate"],
                    date_posted_raw=row["jobPostedDateRaw"],
                    valid_through=row["jobClosureDate"],
                    experience=row["expRange"],
                    employer=EMPLOYER_NAME,
                )
                self.add(job)
                page_rows += 1

            if page_rows == 0 and not parsed["rows"]:
                break
            offset += len(parsed["rows"]) if parsed["rows"] else PAGE_SIZE
            if total is not None and offset >= total:
                break
            if self.limit():
                break

        # Hydrate details.
        async def detail(job):
            code = job.job_id
            url = DETAIL_API.format(job_code=code) + f"?part={DETAIL_PARTS}&isReqId=false"
            data = await self.http.json(url)
            parsed = parse_detail(data, job_code=code)
            # Extract a human-readable experience range if possible.
            experience = parsed["experience"]
            # attach expRange from listing if present in raw
            list_exp = None
            if isinstance(job.raw, dict):
                list_exp = job.raw.get("expRange")
            if list_exp and isinstance(experience, dict):
                experience["range"] = list_exp
            return self.job(
                raw=parsed["raw"],
                title=parsed["title"],
                url=job.url,
                job_id=parsed["job_code"],
                requisition_id=parsed["requisition_id"],
                apply_url=job.url,
                description=parsed["description_html"],
                method="peoplestrong_detail_api",
                description_url=url,
                locations=parsed["locations"] if parsed["locations"] else job.locations,
                department=parsed["department"] or job.department,
                team=parsed["organization_complete"] or job.team,
                employment_type=parsed["employment_type"],
                date_posted=parsed["posted"] or job.date_posted,
                date_posted_raw=job.date_posted_raw,
                valid_through=parsed["valid_through"] or job.valid_through,
                salary=parsed["salary"],
                qualifications=parsed["qualifications"],
                responsibilities=parsed["responsibilities"],
                experience=experience if experience else job.experience,
                education=None,
                employer=parsed["employer"],
                language=None,
            )

        await self.hydrate(self.result.jobs, detail)

        # Fill experience range from listing raw where missing.
        for job in self.result.jobs:
            pass

        self.result.listing_complete = (
            total is not None
            and len(self.result.jobs) == total
            and not any("listing_total_mismatch" in e for e in self.result.errors)
        )
        if total is not None and len(self.result.jobs) != total:
            if "listing_total_mismatch" not in self.result.errors:
                self.result.errors.append("listing_total_mismatch")
        self.result.metadata.update({
            "total_records_reported": total,
            "jobs_collected": len(self.result.jobs),
            "unique_job_codes": len(seen_codes),
            "page_size": PAGE_SIZE,
        })
        self.result.finish()
        return self.result
