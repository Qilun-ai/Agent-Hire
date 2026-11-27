"""PhonePe's own careers journey: company-published feed -> exact SmartRecruiters postings.

The official careers entry (https://www.phonepe.com/careers/) links to the
company's job-openings page. That page's shipped UI bundle fetches
``/apollo/job-postings/latest.json`` and keeps rows where
``status == "PUBLIC" and applyUrl``. Every retained row links to one exact
``jobs.smartrecruiters.com/PHONEPELIMITED/<id>`` detail page. The company feed,
not the wider SmartRecruiters board, is the membership authority here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import re
import time
from urllib.parse import urlsplit

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.phonepe.com/careers/"
LISTING_URL = "https://www.phonepe.com/careers/job-openings/"
FEED_URL = "https://www.phonepe.com/apollo/job-postings/latest.json"
BOARD = "PHONEPELIMITED"
POSTING_API = "https://api.smartrecruiters.com/v1/companies/PHONEPELIMITED/postings/{posting_id}"
DESCRIPTION_METHOD = "phonepe.company-feed.linked-smartrecruiters.full_sections"
SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
APPLY_PATH = re.compile(r"^/([A-Za-z0-9]+)/(\d+)(?:-[^/]*)?$")
DATE_MS = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def parse_feed_date(value):
    """Parse a company feed date without treating a request time as a posting date."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    match = DATE_MS.match(raw)
    try:
        if match:
            stamp = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        else:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            stamp = stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    timespec = "seconds" if not stamp.microsecond else (
        "milliseconds" if stamp.microsecond % 1000 == 0 else "microseconds")
    return stamp.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_apply_url(url: str) -> tuple[str, str]:
    """Return the exact SmartRecruiters board and numeric posting ID from a company link."""
    parts = urlsplit(url or "")
    if parts.scheme != "https" or parts.hostname != "jobs.smartrecruiters.com":
        raise ValueError(f"PhonePe no longer links to jobs.smartrecruiters.com: {url!r}")
    match = APPLY_PATH.match(parts.path)
    if match is None:
        raise ValueError(f"PhonePe apply URL is not a SmartRecruiters posting: {url!r}")
    board, posting_id = match.groups()
    if board != BOARD:
        raise ValueError(f"PhonePe apply URL references unexpected board {board!r}")
    return board, posting_id


def _status_counts(rows):
    counts = {}
    for row in rows:
        status = row.get("status")
        key = "MISSING" if status is None else str(status)
        counts[key] = counts.get(key, 0) + 1
    return counts


def parse_membership(data: dict):
    """Filter the company feed exactly as its published UI bundle does."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("PhonePe feed lost its results array")
    rows = data["results"]
    total = data.get("totalCount")
    if not isinstance(total, int) or total != len(rows):
        raise ValueError(f"PhonePe feed totalCount {total!r} does not match {len(rows)} results")
    members, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "PUBLIC" or not row.get("applyUrl"):
            continue
        _, posting_id = parse_apply_url(row["applyUrl"])
        if posting_id in seen:
            raise ValueError(f"PhonePe feed repeats public posting ID {posting_id}")
        title = str(row.get("title") or "").strip()
        if not title:
            raise ValueError(f"PhonePe public posting {posting_id} has no title")
        seen.add(posting_id)
        members.append(row)
    diagnostics = {
        "listing_url": LISTING_URL,
        "feed_url": FEED_URL,
        "feed_fetched_at": data.get("fetchedAt"),
        "feed_total_count": total,
        "feed_results": len(rows),
        "status_counts": _status_counts(rows),
        "public_with_apply_url": sum(
            1 for row in rows if row.get("status") == "PUBLIC" and row.get("applyUrl")),
        "public_members": len(members),
        "public_without_apply_url": sum(
            1 for row in rows if row.get("status") == "PUBLIC" and not row.get("applyUrl")),
    }
    if diagnostics["public_with_apply_url"] != len(members):
        raise ValueError("PhonePe public membership count changed while parsing")
    return members, diagnostics


def section_records(job_ad) -> list[dict]:
    """Return non-empty section title/HTML records in the published order."""
    sections = (job_ad or {}).get("sections") if isinstance(job_ad, dict) else None
    if not isinstance(sections, dict):
        raise ValueError("SmartRecruiters posting lost jobAd.sections")
    order = list(SECTION_ORDER) + [key for key in sections if key not in SECTION_ORDER]
    records = []
    for key in order:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        body = section.get("text") or ""
        if not text_html(body):
            continue
        title = str(section.get("title") or key).strip()
        records.append({"key": key, "title": title, "text": body})
    return records


def sections_html(records: list[dict]) -> str:
    return "\n".join(
        f"<h2>{escape(record['title'])}</h2>{record['text']}" for record in records)


def _custom_field(row, label):
    for field in row.get("customField") or []:
        if isinstance(field, dict) and field.get("fieldLabel") == label:
            return field.get("valueLabel")
    return None


def detail_fields(row: dict, feed_row: dict | None = None, *, expected_id: str | None = None) -> dict:
    """Map one exact SmartRecruiters posting record, including empty descriptions."""
    if not isinstance(row, dict):
        raise ValueError("SmartRecruiters posting response is not an object")
    posting_id = str(row.get("id") or "")
    if not posting_id.isdigit():
        raise ValueError("SmartRecruiters posting has no numeric id")
    if expected_id is not None and posting_id != str(expected_id):
        raise ValueError(f"SmartRecruiters returned posting {posting_id}, expected {expected_id}")
    company = row.get("company") or {}
    if company.get("identifier") != BOARD:
        raise ValueError(f"SmartRecruiters posting {posting_id} belongs to {company.get('identifier')!r}")
    if row.get("visibility") != "PUBLIC" or row.get("active") is not True:
        raise ValueError(f"SmartRecruiters posting {posting_id} is no longer public and active")
    records = section_records(row.get("jobAd"))
    html = sections_html(records)
    description = text_html(html)
    unavailable = not bool(description.strip())
    qualifications = next((r["text"] for r in records if r["key"] == "qualifications"), None)
    responsibilities = next((r["text"] for r in records if r["key"] == "jobDescription"), None)
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    feed_row = feed_row or {}
    employment = (row.get("typeOfEmployment") or {}).get("label") or feed_row.get("type")
    department = _custom_field(row, "Department Name") or feed_row.get("department")
    workplace = "Remote" if location.get("remote") else "Hybrid" if location.get("hybrid") else None
    return {
        "title": str(row.get("name") or feed_row.get("title") or "").strip(),
        "job_id": posting_id,
        "requisition_id": row.get("refNumber") or None,
        "apply_url": row.get("applyUrl") or feed_row.get("applyUrl"),
        "description_html": html,
        "description_text": description,
        "description_method": DESCRIPTION_METHOD,
        "description_source_url": POSTING_API.format(posting_id=posting_id),
        "description_unavailable": unavailable,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "locations": [location] if location else ([feed_row["location"]] if feed_row.get("location") else []),
        "department": department,
        "employment_type": employment,
        "workplace_type": workplace,
        "date_posted": parse_feed_date(row.get("releasedDate")),
        "date_posted_raw": row.get("releasedDate"),
        "date_updated": parse_feed_date(feed_row.get("updatedAt")),
        "employer": company.get("name") or None,
        "experience": (row.get("experienceLevel") or {}).get("label") or None,
        "language": (row.get("language") or {}).get("label") or None,
        "salary": None,
        "warnings": ["source_description_missing"] if unavailable else [],
    }


class PhonePe(Adapter):
    async def run(self):
        listing_started = time.perf_counter()
        data = await self.http.json(FEED_URL)
        members, diagnostics = parse_membership(data)
        self.result.metadata["listing"] = diagnostics
        self.result.pages = 1
        self.result.expected_total = len(members)
        for row in members:
            _, posting_id = parse_apply_url(row["applyUrl"])
            self.add(self.job(
                row, title=str(row.get("title") or "").strip(), job_id=posting_id,
                url=row["applyUrl"], apply_url=row["applyUrl"],
                locations=[row["location"]] if row.get("location") else [],
                department=row.get("department"), employment_type=row.get("type"),
                date_updated=parse_feed_date(row.get("updatedAt")),
            ))
            if self.limit():
                break
        self.result.listing_complete = (
            "sample_limit" not in self.result.warnings and len(self.seen) == len(members))
        self.result.timings["listing_phase_seconds"] = max(
            0.0, time.perf_counter() - listing_started)

        async def detail(job):
            _, posting_id = parse_apply_url(job.url)
            api = POSTING_API.format(posting_id=posting_id)
            row = await self.http.json(api)
            job.raw["detail"] = row
            fields = detail_fields(row, job.raw, expected_id=posting_id)
            for key, value in fields.items():
                if key == "warnings":
                    job.warnings.extend(value)
                else:
                    setattr(job, key, value)
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        empty = [
            {"id": job.job_id, "url": job.url, "title": job.title,
             "reason": "The official SmartRecruiters posting and its detail page publish empty "
                       "Company Description/Job Description/Qualifications/Additional Information sections."}
            for job in self.result.jobs if job.description_unavailable
        ]
        if empty:
            self.result.metadata["empty_source_descriptions"] = {"count": len(empty), "jobs": empty}
            self.result.warnings.append("source_descriptions_empty")
            self.result.errors.append(f"empty_source_descriptions:{len(empty)}")
        return self.result
