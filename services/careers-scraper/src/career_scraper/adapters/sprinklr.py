"""Sprinklr's official careers page and the exact Workday board it publishes.

The Sprinklr careers page links its "Explore jobs" call to action to
``https://sprinklr.wd1.myworkdayjobs.com/careers``.  That company-published
Workday site exposes 20 postings per page and requests its first page from the
public CXS endpoint.  This adapter verifies both the Sprinklr entry page and the
Workday tenant/site shell before following only the unfiltered IDs that board
publishes.
"""

from __future__ import annotations

from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.sprinklr.com/careers/"
OFFICIAL_ENTRY_TITLE = "Sprinklr Careers"
OFFICIAL_ENTRY_H1 = "Be part of a team that makes extraordinary possible."
OFFICIAL_ENTRY_DESCRIPTION = (
    "Find available career opportunities at Sprinklr. Sprinklr is proud to be an "
    "equal-opportunity workplace and is an affirmative action employer. Apply today."
)
EXPLORE_LABEL = "Explore jobs"
WORKDAY_HOST = "sprinklr.wd1.myworkdayjobs.com"
WORKDAY_ORIGIN = "https://sprinklr.wd1.myworkdayjobs.com"
WORKDAY_SITE = "careers"
WORKDAY_BOARD = WORKDAY_ORIGIN + "/careers"
WORKDAY_API = f"{WORKDAY_ORIGIN}/wday/cxs/sprinklr/{WORKDAY_SITE}"
WORKDAY_JOBS_API = WORKDAY_API + "/jobs"
WORKDAY_TENANT = "sprinklr"
PAGE_SIZE = 20
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQ_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TEXT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
SALARY_RANGE = re.compile(
    r"^(?P<currency>[$€£])\s*(?P<minimum>\d[\d,]*)(?:\s*[-–]\s*"
    r"(?P<currency2>[$€£])?\s*(?P<maximum>\d[\d,]*))?$"
)
CURRENCY_CODES = {"$": "USD", "€": "EUR", "£": "GBP"}
SECTION_HEADINGS = {
    "job description",
    "about this role",
    "about the role",
    "meet our professional services team!",
    "meet our team!",
    "what does success look like",
    "key responsibilities",
    "main responsibilities",
    "responsibilities",
    "what you'll do",
    "what you’ll do",
    "what you will do",
    "what makes you qualified?",
    "what makes you qualified",
    "desired skills and experience",
    "qualifications",
    "requirements",
    "who you are",
    "what we're looking for",
    "what we’re looking for",
    "nice to have",
    "preferred qualifications",
    "about you",
    "job req compensation range",
    "we focus on our mission",
    "we believe in our product",
    "we invest in our people",
    "eeo - our philosophy",
    "warning about recruiting scams",
    "warning about recruiting scams:",
}
RESPONSIBILITY_HEADINGS = {
    "key responsibilities",
    "main responsibilities",
    "responsibilities",
    "what you'll do",
    "what you’ll do",
    "what you will do",
}
QUALIFICATION_HEADINGS = {
    "what makes you qualified?",
    "what makes you qualified",
    "desired skills and experience",
    "qualifications",
    "requirements",
    "who you are",
    "what we're looking for",
    "what we’re looking for",
    "nice to have",
    "preferred qualifications",
    "about you",
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """Normalise a rendered body for an exact visible-text comparison."""
    text = unicodedata.normalize("NFC", value or "")
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _canonical_board_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Verify Sprinklr's own careers page and the board its visible CTA publishes."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.sprinklr.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Sprinklr official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    description_node = soup.select_one('meta[name="description"]')
    description = normalized_title(description_node.get("content") if description_node else "")
    h1 = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    if title != OFFICIAL_ENTRY_TITLE:
        raise ValueError("Sprinklr official careers page title changed")
    if description != OFFICIAL_ENTRY_DESCRIPTION:
        raise ValueError("Sprinklr official careers page description changed")
    if h1 != [OFFICIAL_ENTRY_H1]:
        raise ValueError("Sprinklr official careers page heading changed")
    if _canonical_board_url(canonical_url) != OFFICIAL_ENTRY.rstrip("/"):
        raise ValueError("Sprinklr official careers page canonical URL changed")

    board_links: list[str] = []
    for anchor in soup.select("a[href]"):
        if normalized_title(anchor.get_text(" ", strip=True)) != EXPLORE_LABEL:
            continue
        target = urljoin(response_url, anchor.get("href") or "")
        parts = urlsplit(target)
        if (parts.scheme != "https" or parts.netloc != WORKDAY_HOST
                or parts.path.rstrip("/") != "/careers"):
            raise ValueError("Sprinklr Explore jobs link no longer points at its Workday careers site")
        board_links.append(_canonical_board_url(target))
    if not board_links:
        raise ValueError("Sprinklr official careers page no longer exposes its Explore jobs CTA")
    if set(board_links) != {WORKDAY_BOARD}:
        raise ValueError("Sprinklr Explore jobs CTAs do not share one canonical Workday board")
    return {
        "url": response_url,
        "title": title,
        "description": description,
        "h1": h1,
        "canonical_url": canonical_url,
        "explore_label": EXPLORE_LABEL,
        "explore_links": sorted(set(board_links)),
        "workday_board_url": WORKDAY_BOARD,
    }


def parse_workday_shell(html: str, response_url: str = WORKDAY_BOARD) -> dict:
    """Read and verify the tenant/site identity from Workday's public page shell."""
    parsed = urlsplit(response_url)
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    tenant = re.search(r'\btenant\s*:\s*["\']([^"\']+)["\']', html)
    site = re.search(r'\bsiteId\s*:\s*["\']([^"\']+)["\']', html)
    locale = re.search(r'\brequestLocale\s*:\s*["\']([^"\']+)["\']', html)
    identity = {
        "host": parsed.hostname or "",
        "path": parsed.path,
        "canonical_url": canonical_url,
        "tenant": tenant.group(1) if tenant else "",
        "site_id": site.group(1) if site else "",
        "request_locale": locale.group(1) if locale else "",
    }
    if (parsed.scheme != "https" or parsed.hostname != WORKDAY_HOST
            or parsed.path.rstrip("/") != "/careers"
            or _canonical_board_url(canonical_url) != WORKDAY_BOARD
            or identity["tenant"] != WORKDAY_TENANT
            or identity["site_id"] != WORKDAY_SITE):
        raise ValueError("Sprinklr's published Workday careers site identity changed")
    return identity


def parse_listing_page(
    value: dict,
    requested_offset: int,
    *,
    limit: int = PAGE_SIZE,
    expected_total: int | None = None,
) -> dict:
    """Parse one unfiltered Sprinklr CXS page and preserve the Workday total quirk."""
    if not isinstance(value, dict):
        raise ValueError("Sprinklr Workday listing response is not an object")
    total = value.get("total")
    rows = value.get("jobPostings")
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0
            or not isinstance(rows, list)):
        raise ValueError("Sprinklr Workday listing response is missing total or jobPostings")
    if len(rows) > limit:
        raise ValueError("Sprinklr Workday listing returned more rows than requested")
    issues: list[str] = []
    if requested_offset == 0:
        if expected_total is not None and total != expected_total:
            issues.append(f"sprinklr_listing_total_changed:{expected_total}->{total}")
    elif expected_total is not None and total not in (0, expected_total):
        # Later Sprinklr CXS pages report total=0 while still returning rows.
        issues.append(f"sprinklr_listing_later_page_total_changed:{expected_total}->{total}")
    if expected_total is not None:
        expected_rows = min(limit, max(0, expected_total - requested_offset))
        if len(rows) != expected_rows:
            issues.append(
                f"sprinklr_listing_page_row_count:{requested_offset}:{len(rows)}/{expected_rows}"
            )

    parsed_rows, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("sprinklr_listing_row_not_object")
            continue
        bullets = row.get("bulletFields")
        identity = str(bullets[0]).strip() if isinstance(bullets, list) and bullets else ""
        path = row.get("externalPath")
        if not REQ_ID.fullmatch(identity) or not isinstance(path, str) or not path.startswith("/job/"):
            issues.append(f"sprinklr_listing_row_identity_missing:{identity or '?'}")
            continue
        if identity in seen:
            issues.append(f"sprinklr_listing_duplicate_id:{identity}")
            continue
        if identity not in path:
            issues.append(f"sprinklr_listing_requisition_not_in_path:{identity}")
        if not normalized_title(row.get("title")):
            issues.append(f"sprinklr_listing_row_title_missing:{identity}")
        if not normalized_title(row.get("locationsText")):
            issues.append(f"sprinklr_listing_row_location_missing:{identity}")
        if not isinstance(row.get("postedOn"), str):
            issues.append(f"sprinklr_listing_row_posted_on_missing:{identity}")
        seen.add(identity)
        parsed_rows.append(dict(row))
    return {
        "rows": parsed_rows,
        "job_ids": [str(row["bulletFields"][0]) for row in parsed_rows],
        "path": [row["externalPath"] for row in parsed_rows],
        "total": total,
        "requested_offset": requested_offset,
        "limit": limit,
        "expected_total": expected_total,
        "issues": issues,
    }


def _description_lines(html: str | None) -> list[str]:
    return [line.strip() for line in text_html(html).splitlines() if line.strip()]


def description_sections(html: str | None) -> list[dict]:
    """Keep the employer's labelled sections while preserving the full HTML separately."""
    sections: list[dict] = []
    current = {"heading": None, "text": ""}
    for line in _description_lines(html):
        heading = line[:-1].strip() if line.endswith(":") else line
        key = heading.casefold()
        if key in SECTION_HEADINGS and len(heading) <= 120:
            if current["heading"] or current["text"].strip():
                sections.append(current)
            current = {"heading": heading, "text": ""}
            continue
        current["text"] = f"{current['text']}\n{line}".strip()
    if current["heading"] or current["text"].strip():
        sections.append(current)
    return [section for section in sections if section["heading"] or section["text"]]


def extract_salary(html: str | None) -> dict | None:
    lines = _description_lines(html)
    for index, line in enumerate(lines):
        if not re.search(r"(?:compensation|salary|pay)\s+range", line, re.I):
            continue
        for candidate in lines[index + 1:index + 4]:
            match = SALARY_RANGE.fullmatch(candidate.strip())
            if not match:
                continue
            minimum = int(match.group("minimum").replace(",", ""))
            maximum = int(match.group("maximum").replace(",", "")) if match.group("maximum") else None
            currency = CURRENCY_CODES.get(match.group("currency") or "", None)
            return {
                "raw": candidate.strip(),
                "currency": currency,
                "minimum": minimum,
                "maximum": maximum,
                "period": None,
                "source_heading": line.strip(),
                "source_field": "jobPostingInfo.jobDescription",
            }
    return None


def _section_text(sections: list[dict], headings: set[str]) -> str | None:
    parts = [
        section["text"].strip()
        for section in sections
        if (section.get("heading") or "").casefold() in headings and section["text"].strip()
    ]
    return "\n\n".join(parts) if parts else None


def parse_detail(value: dict, expected: dict) -> dict:
    """Parse the exact CXS detail for one listing row and audit its identity."""
    if not isinstance(value, dict) or not isinstance(value.get("jobPostingInfo"), dict):
        raise ValueError("Sprinklr Workday detail response is missing jobPostingInfo")
    info = value["jobPostingInfo"]
    expected_id = str(expected["bulletFields"][0])
    expected_path = str(expected["externalPath"])
    expected_title = normalized_title(expected.get("title"))
    identity = str(info.get("jobReqId") or "")
    fatal: list[str] = []
    warnings: list[str] = []

    if identity != expected_id:
        fatal.append(f"requisition_mismatch:{expected_id}:{identity or '?'}")
    if info.get("jobPostingSiteId") != WORKDAY_SITE:
        fatal.append(f"site_mismatch:{expected_id}")
    if str(info.get("externalUrl") or "") != WORKDAY_BOARD + expected_path:
        fatal.append(f"external_url_mismatch:{expected_id}")
    expected_posting_id = expected_path.rsplit("/", 1)[-1]
    if str(info.get("jobPostingId") or "") != expected_posting_id:
        fatal.append(f"posting_id_mismatch:{expected_id}")
    if normalized_title(info.get("title")) != expected_title:
        fatal.append(f"title_mismatch:{expected_id}")
    if info.get("posted") is not True:
        fatal.append(f"not_posted:{expected_id}")

    description = info.get("jobDescription")
    if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
        fatal.append(f"description_empty_or_truncated:{expected_id}")

    start_date = info.get("startDate")
    if start_date is not None and (not isinstance(start_date, str) or not DATE_ONLY.fullmatch(start_date)):
        warnings.append(f"start_date_invalid:{expected_id}")

    additional = info.get("additionalLocations")
    if additional is not None and (
        not isinstance(additional, list)
        or any(not isinstance(item, str) or not item.strip() for item in additional)
    ):
        warnings.append(f"additional_locations_invalid:{expected_id}")

    location = info.get("location")
    if not isinstance(location, str) or not normalized_title(location):
        fatal.append(f"location_missing:{expected_id}")
    if not normalized_title(info.get("timeType")):
        warnings.append(f"time_type_missing:{expected_id}")
    if not isinstance(info.get("country"), dict):
        warnings.append(f"country_missing:{expected_id}")

    sections = description_sections(description if isinstance(description, str) else "")
    locations = []
    if isinstance(location, str) and normalized_title(location):
        locations.append(location)
    if isinstance(additional, list):
        locations.extend(item for item in additional if isinstance(item, str) and item.strip())
    deduped_locations = list(dict.fromkeys(locations))
    can_apply = info.get("canApply") is True
    if not can_apply:
        warnings.append(f"not_applyable:{expected_id}")
    external_url = str(info.get("externalUrl") or "")
    return {
        "id": identity or expected_id,
        "job_req_id": identity or None,
        "title": normalized_title(info.get("title")),
        "external_url": external_url,
        "job_posting_id": info.get("jobPostingId"),
        "job_posting_site_id": info.get("jobPostingSiteId"),
        "location": location,
        "locations": deduped_locations,
        "additional_locations": list(additional or []) if isinstance(additional, list) else [],
        "country": info.get("country"),
        "job_requisition_location": info.get("jobRequisitionLocation"),
        "time_type": info.get("timeType"),
        "start_date": start_date if isinstance(start_date, str) and DATE_ONLY.fullmatch(start_date) else None,
        "posted_on": info.get("postedOn"),
        "end_date": info.get("endDate"),
        "can_apply": can_apply,
        "posted": info.get("posted"),
        "questionnaire_id": info.get("questionnaireId"),
        "include_resume_parsing": info.get("includeResumeParsing"),
        "job_description": description if isinstance(description, str) else "",
        "description_text": text_html(description if isinstance(description, str) else ""),
        "sections": sections,
        "qualifications": _section_text(sections, QUALIFICATION_HEADINGS),
        "responsibilities": _section_text(sections, RESPONSIBILITY_HEADINGS),
        "salary": extract_salary(description if isinstance(description, str) else ""),
        "apply_url": (external_url + "/apply") if can_apply and external_url else None,
        "fatal_issues": fatal,
        "warnings": warnings,
        "identity_checks": {
            "requisition_matches_listing_bullet": identity == expected_id,
            "site_matches": info.get("jobPostingSiteId") == WORKDAY_SITE,
            "external_url_matches_listing_path": str(info.get("externalUrl") or "") == WORKDAY_BOARD + expected_path,
            "posting_id_matches_path": str(info.get("jobPostingId") or "") == expected_posting_id,
            "title_matches_listing": normalized_title(info.get("title")) == expected_title,
            "posted_is_true": info.get("posted") is True,
            "description_present": isinstance(description, str) and bool(normalized_body_text(description)),
        },
        "info": info,
    }


class Sprinklr(Adapter):
    """Collect the exact unfiltered postings from Sprinklr's published Workday board."""

    async def _listing_page(self, offset: int, *, expected_total: int | None = None) -> dict:
        if not self.page_guard():
            raise ValueError("Sprinklr listing page budget exhausted")
        payload = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        data = await self.http.json(
            WORKDAY_JOBS_API,
            method="POST",
            json=payload,
            headers={"Accept": "application/json"},
        )
        return parse_listing_page(
            data, offset, limit=PAGE_SIZE, expected_total=expected_total
        )

    def _add_listing_row(self, row: dict) -> None:
        identity = str(row["bulletFields"][0])
        path = row["externalPath"]
        url = WORKDAY_BOARD + path
        self.add(
            self.job(
                {"listing": row},
                title=row.get("title"),
                job_id=identity,
                url=url,
                locations=[row.get("locationsText")],
                date_posted_raw={"postedOn": row.get("postedOn")},
            )
        )

    async def _detail(self, job):
        listing = job.raw["listing"]
        path = listing["externalPath"]
        api_url = WORKDAY_API + path
        value = await self.http.json(api_url, headers={"Accept": "application/json"})
        parsed = parse_detail(value, listing)
        job.raw["detail"] = value
        job.raw["detail_url"] = api_url
        job.raw["detail_identity_audit"] = parsed["identity_checks"]
        for warning in parsed["warnings"]:
            job.warnings.append(f"sprinklr_{warning}")
            self.result.warnings.append(f"sprinklr_{warning}")
        if parsed["fatal_issues"]:
            for issue in parsed["fatal_issues"]:
                self.result.errors.append(f"sprinklr_detail_{issue}")
            job.warnings.append("sprinklr_exact_detail_identity_unverified")
            job.description_unavailable = True
            return job

        job.title = parsed["title"] or job.title
        job.job_id = parsed["job_req_id"] or job.job_id
        job.requisition_id = parsed["job_req_id"] or job.requisition_id
        job.url = parsed["external_url"] or job.url
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["job_description"]
        job.description_text = parsed["description_text"]
        job.description_method = "workday.cxs.jobPostingInfo.jobDescription"
        job.description_source_url = api_url
        job.date_posted = parsed["start_date"]
        job.date_posted_raw = {
            "postedOn": parsed["posted_on"],
            "startDate": parsed["start_date"],
        }
        job.valid_through = (
            parsed["end_date"]
            if isinstance(parsed["end_date"], str) and DATE_ONLY.fullmatch(parsed["end_date"])
            else None
        )
        job.locations = parsed["locations"]
        job.employment_type = parsed["time_type"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.salary = parsed["salary"]
        job.raw["description_sections"] = parsed["sections"]
        return job

    async def _final_membership(self, expected_total: int, initial_paths: list[str]) -> dict:
        started = time.perf_counter()
        final_paths: list[str] = []
        page_offsets: list[int] = []
        page_errors: list[str] = []
        final_total: int | None = None
        offset = 0
        while len(final_paths) < expected_total:
            page = await self._listing_page(offset, expected_total=expected_total)
            if offset == 0:
                final_total = page["total"]
            page_offsets.append(offset)
            page_errors.extend(page["issues"])
            for row in page["rows"]:
                path = row["externalPath"]
                if path in final_paths:
                    page_errors.append(f"sprinklr_final_duplicate_path:{path}")
                final_paths.append(path)
            if not page["rows"]:
                page_errors.append(f"sprinklr_final_empty_page_at_offset:{offset}")
                break
            offset += len(page["rows"])
            if offset > expected_total + PAGE_SIZE:
                page_errors.append("sprinklr_final_listing_exceeded_expected_membership")
                break
        unique_paths = set(final_paths)
        if len(unique_paths) != len(final_paths):
            page_errors.append("sprinklr_final_listing_duplicate_paths")
        if len(unique_paths) != expected_total:
            page_errors.append(
                f"sprinklr_final_listing_membership_count:{len(unique_paths)}/{expected_total}"
            )
        if final_total != expected_total:
            page_errors.append(f"sprinklr_final_total_changed:{expected_total}->{final_total}")
        same = set(initial_paths) == unique_paths
        if not same:
            page_errors.append("sprinklr_initial_final_membership_changed")
        return {
            "total": final_total,
            "page_size": PAGE_SIZE,
            "page_offsets": page_offsets,
            "page_count": len(page_offsets),
            "returned_rows": len(final_paths),
            "unique_count": len(unique_paths),
            "external_paths": sorted(unique_paths),
            "external_paths_sha256": sha256("\n".join(sorted(unique_paths)).encode()).hexdigest(),
            "same_id_set_as_initial": same,
            "errors": page_errors,
            "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def run(self):
        if self.company.careers_url != OFFICIAL_ENTRY or self.source.url != OFFICIAL_ENTRY:
            raise ValueError("Sprinklr adapter must start at its official careers entry")

        entry_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": TEXT_ACCEPT})
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        self.result.timings["official_entry_seconds"] = round(
            max(0.0, time.perf_counter() - entry_started), 6
        )

        shell_started = time.perf_counter()
        shell_response = await self.http.get(WORKDAY_BOARD, headers={"Accept": TEXT_ACCEPT})
        site_identity = parse_workday_shell(shell_response.text, str(shell_response.url))
        self.result.timings["workday_shell_seconds"] = round(
            max(0.0, time.perf_counter() - shell_started), 6
        )
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_identity": entry,
            "published_link_label": EXPLORE_LABEL,
            "workday_board_url": WORKDAY_BOARD,
            "workday_site_identity": site_identity,
            "listing_authority": "Sprinklr official Explore jobs journey to its unfiltered Workday careers site",
            "request_source": "Published Workday CXS unfiltered jobs listing and exact listed-job detail requests",
            "native_page_size": PAGE_SIZE,
            "listing_total_quirk": "Later Sprinklr CXS pages may report total=0 while returning their rows; the first page total is authoritative.",
        })

        listing_started = time.perf_counter()
        offset = 0
        rows: list[dict] = []
        first_total: int | None = None
        page_offsets: list[int] = []
        page_errors: list[str] = []
        while True:
            page = await self._listing_page(offset, expected_total=first_total)
            if first_total is None:
                first_total = page["total"]
            page_offsets.append(offset)
            page_errors.extend(page["issues"])
            rows.extend(page["rows"])
            if not page["rows"]:
                page_errors.append(f"sprinklr_empty_page_at_offset:{offset}")
                break
            offset += len(page["rows"])
            if first_total == 0:
                break
            if offset >= first_total:
                break
            if offset > first_total + PAGE_SIZE:
                page_errors.append("sprinklr_listing_exceeded_expected_membership")
                break

        unique_rows: list[dict] = []
        seen_ids: set[str] = set()
        for row in rows:
            identity = str(row["bulletFields"][0])
            if identity in seen_ids:
                page_errors.append(f"sprinklr_listing_duplicate_id:{identity}")
                continue
            seen_ids.add(identity)
            unique_rows.append(row)
        if first_total is None:
            page_errors.append("sprinklr_initial_listing_has_no_total")
        elif first_total == 0:
            # A zero from this endpoint cannot silently certify an empty board;
            # the rendered listing must be observed before calling it empty.
            page_errors.append("sprinklr_empty_listing_requires_current_rendered_confirmation")
        if first_total is not None and len(unique_rows) != first_total:
            page_errors.append(
                f"sprinklr_initial_listing_membership_count:{len(unique_rows)}/{first_total}"
            )

        self.result.expected_total = first_total
        for row in unique_rows:
            self._add_listing_row(row)
        initial_paths = sorted(row["externalPath"] for row in unique_rows)
        collection_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.timings["initial_listing_seconds"] = round(collection_seconds, 6)
        self.result.metadata["initial_listing"] = {
            "total": first_total,
            "page_size": PAGE_SIZE,
            "page_offsets": page_offsets,
            "page_count": len(page_offsets),
            "returned_rows": len(rows),
            "unique_rows": len(unique_rows),
            "external_paths": initial_paths,
            "external_paths_sha256": sha256("\n".join(initial_paths).encode()).hexdigest(),
            "issues": page_errors,
        }
        if page_errors:
            self.result.errors.extend(page_errors)
            self.result.listing_complete = False
        elif len(self.result.jobs) == first_total and first_total:
            self.result.listing_complete = True
        else:
            self.result.listing_complete = False

        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - listing_started), 6
        )

        if first_total:
            final = await self._final_membership(first_total, initial_paths)
            self.result.timings["final_membership_seconds"] = final.pop("duration_seconds")
            final["initial_unique_count"] = len(initial_paths)
            final["initial_external_paths"] = initial_paths
            final["initial_external_paths_sha256"] = sha256(
                "\n".join(initial_paths).encode()
            ).hexdigest()
            self.result.metadata["membership_reconciliation"] = final
            if final["errors"]:
                self.result.errors.extend(final["errors"])
                self.result.listing_complete = False
        else:
            self.result.metadata["membership_reconciliation"] = {
                "total": first_total,
                "same_id_set_as_initial": False,
                "errors": ["sprinklr_empty_listing_requires_current_rendered_confirmation"],
            }
        self.result.metadata["initial_page_count"] = len(page_offsets)
        self.result.metadata["initial_expected_total"] = first_total
        return self.result
