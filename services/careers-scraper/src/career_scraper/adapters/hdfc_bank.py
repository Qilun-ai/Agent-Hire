"""HDFC Bank's own careers page and its company-scoped RippleHire job board."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.hdfcbank.com/personal/about-us/careers"
OFFICIAL_FINAL_EXPECTED = "https://www.hdfc.bank.in/careers"
BOARD_LINK = "https://hdfcbank.ripplehire.com/candidate/careers"
BOARD_ORIGIN = "https://hdfcbank.ripplehire.com"
LISTING_PATH = "/candidate/"
SEARCH_API = BOARD_ORIGIN + "/candidate/candidatejobsearch"
DETAIL_API = BOARD_ORIGIN + "/candidate/candidatejobdetail"
CAREER_SOURCE = "CAREERSITE"
COMPANY_CD = "HDFCBK"
COMPANY_NAME = "HDFC Bank"
COMPANY_SEQ = "463"
BOARD_DOMAIN = "hdfcbank.ripplehire.com"
PAGE_SIZE = 10  # The board's observed default and "View more" page size.
UNFILTERED_SEARCH = "*:*"
LANG = "en"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_source_date(value: str | None) -> str | None:
    """Parse only the complete dates the source actually publishes."""
    value = (value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", value)
    if match:
        day, month, year = match.groups()
        month_number = _MONTHS.get(month.lower())
        if month_number is None:
            raise ValueError(f"HDFC Bank job date has an unknown month: {value}")
        return datetime(int(year), month_number, int(day), tzinfo=timezone.utc).date().isoformat()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", value)
    if match:
        return datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc).date().isoformat()
    raise ValueError(f"HDFC Bank job date has an unobserved format: {value}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc not in {"www.hdfc.bank.in", "www.hdfcbank.com"}:
        raise ValueError("HDFC Bank official careers entry redirected outside the bank's own domain")
    if parts.path.rstrip("/") != "/careers":
        raise ValueError("HDFC Bank official careers entry is no longer the inspected /careers route")
    soup = BeautifulSoup(html, "lxml")
    links = []
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor["href"])
        target = urlsplit(href)
        if (target.scheme == "https" and target.netloc == BOARD_DOMAIN
                and target.path.rstrip("/") == "/candidate/careers"):
            links.append(href.split("#", 1)[0])
    if not links:
        raise ValueError("HDFC Bank official careers page no longer links its RippleHire job board")
    if any(link != BOARD_LINK for link in links):
        raise ValueError("HDFC Bank official careers page links an unexpected RippleHire board URL")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "HDFC Bank Careers" not in title:
        raise ValueError("HDFC Bank official careers page title changed")
    return {
        "url": response_url,
        "final_url": response_url,
        "title": title,
        "board_link": BOARD_LINK,
        "board_link_occurrences": len(links),
        "apply_now_ctas": len(links),
    }


def parse_board_shell(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != BOARD_DOMAIN:
        raise ValueError("HDFC Bank board redirect left the company-scoped RippleHire host")
    if parsed.path != LISTING_PATH:
        raise ValueError("HDFC Bank board redirect is not the inspected /candidate/ route")
    query = dict(item.split("=", 1) for item in parsed.query.split("&") if "=" in item)
    token = query.get("token", "")
    if not re.fullmatch(r"[A-Za-z0-9]+", token):
        raise ValueError("HDFC Bank board redirect did not expose a usable company token")
    if query.get("source") != CAREER_SOURCE:
        raise ValueError("HDFC Bank board redirect source changed from CAREERSITE")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "HDFC Bank Careers" not in title or "Ripplehire" not in title:
        raise ValueError("HDFC Bank board page title no longer identifies the company board")
    hidden = {element.get("id"): element.get("value") for element in soup.select("input[type=hidden][id]")}
    if hidden.get("token") != token or hidden.get("source") != CAREER_SOURCE:
        raise ValueError("HDFC Bank board page hidden token/source do not match its route")
    if "HDFC-bank" not in html:
        raise ValueError("HDFC Bank board page no longer exposes the HDFCBK company branding")
    listing_url = f"{BOARD_ORIGIN}{LISTING_PATH}?token={token}&source={CAREER_SOURCE}#list"
    return {
        "url": BOARD_LINK,
        "final_url": response_url,
        "listing_url": listing_url,
        "token": token,
        "source": CAREER_SOURCE,
        "title": title,
        "hidden_token_matches": True,
    }


def parse_listing_page(value: dict, requested_page: int, *, page_size: int = PAGE_SIZE,
                       expected_total: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("HDFC Bank listing API response is not an object")
    start = value.get("startJobIndex")
    maximum = value.get("maxJobSize")
    total = value.get("totalJobCount")
    rows = value.get("jobVoList")
    if (not isinstance(start, int) or not isinstance(maximum, int) or not isinstance(total, int)
            or total < 0 or not isinstance(rows, list)):
        raise ValueError("HDFC Bank listing API pagination schema changed")
    if maximum != page_size:
        raise ValueError("HDFC Bank listing API page size changed from the observed request")
    if start != requested_page * page_size:
        raise ValueError("HDFC Bank listing API returned a different page offset")
    if expected_total is not None and total != expected_total:
        raise ValueError(f"HDFC Bank listing total changed from {expected_total} to {total}")
    expected_rows = min(page_size, max(0, total - start))
    issues = []
    if len(rows) != expected_rows:
        issues.append("hdfc_listing_page_row_count_mismatch")
    identities, declared = [], []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("hdfc_listing_row_not_object")
            continue
        identity = str(row.get("jobId") or row.get("jobSeq") or "")
        if not re.fullmatch(r"\d+", identity):
            issues.append("hdfc_listing_row_missing_numeric_job_id")
            continue
        if str(row.get("jobSeq") or "") not in ("", identity):
            issues.append(f"hdfc_listing_row_job_id_mismatch:{identity}")
            continue
        if not normalized(row.get("jobTitle")):
            issues.append(f"hdfc_listing_row_missing_title:{identity}")
            continue
        if row.get("allowCareerSite") != "Y":
            issues.append(f"hdfc_listing_row_not_public_career_site:{identity}")
            continue
        if identity in identities:
            issues.append(f"hdfc_listing_duplicate_job_id:{identity}")
            continue
        identities.append(identity)
        declared.append(row)
    return {
        "rows": declared,
        "ids": identities,
        "total": total,
        "page": requested_page,
        "page_size": page_size,
        "start": start,
        "issues": list(dict.fromkeys(issues)),
    }


def description_sections(html: str | None) -> list[dict]:
    """Split the employer HTML at its bold-only heading paragraphs."""
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.select_one(".docx_html_scope")
    if root is None:
        body = soup.body or soup
        children = [child for child in body.children if isinstance(child, Tag)]
        if len(children) == 1 and children[0].name in {"div", "section", "article"}:
            root = children[0]
        else:
            root = body
    sections, current = [], None
    for block in (root.contents if root else []):
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in _HEADING_TAGS:
            heading = normalized(block.get_text(" ", strip=True))
        elif block.name in {"p", "div"}:
            text = normalized(block.get_text(" ", strip=True))
            strings = [node for node in block.find_all(string=True) if node.strip()]
            if (text and len(text) <= 140 and strings
                    and all(node.find_parent(["b", "strong"]) is not None for node in strings)):
                heading = text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections or [{"heading": None, "html": "", "text": ""}]


def _section_text(sections: list[dict], pattern: str) -> str | None:
    values = [section["text"].strip() for section in sections
              if section.get("heading") and re.search(pattern, section["heading"], re.I) and section.get("text", "").strip()]
    return "\n\n".join(dict.fromkeys(values)) if values else None


def _section_block(sections: list[dict], start_pattern: str, stop_pattern: str) -> str | None:
    start = next((index for index, section in enumerate(sections)
                  if section.get("heading") and re.search(start_pattern, section["heading"], re.I)), None)
    if start is None:
        return None
    stop = next((index for index in range(start + 1, len(sections))
                 if sections[index].get("heading") and re.search(stop_pattern, sections[index]["heading"], re.I)),
                len(sections))
    values = [section["text"].strip() for section in sections[start:stop] if section.get("text", "").strip()]
    return "\n\n".join(values) if values else None


def locations_from_detail(job: dict) -> list[str]:
    """The board publishes city lists as comma-separated `locations`."""
    values: list[str] = []
    published = job.get("locations")
    if isinstance(published, str) and published.strip():
        values.extend(part.strip() for part in published.split(",") if part.strip())
    elif isinstance(job.get("jobLocationList"), list):
        for item in job["jobLocationList"]:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            elif isinstance(item, dict):
                for key in ("location", "name", "locationName", "city"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        values.append(item[key].strip())
                        break
    else:
        for key in ("jobLocationList", "jobLocation"):
            value = job.get(key)
            if isinstance(value, str) and value.strip():
                values.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(values))


def parse_job_detail(value: dict, listing_row: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("HDFC Bank job detail API response is not an object")
    company = value.get("companyVO")
    job = value.get("jobVO")
    if not isinstance(company, dict) or not isinstance(job, dict):
        raise ValueError("HDFC Bank job detail API response is missing companyVO or jobVO")
    if normalized(company.get("companyName")) != COMPANY_NAME:
        raise ValueError("HDFC Bank job detail company identity changed")
    if company.get("companyCd") != COMPANY_CD or company.get("domainName") != BOARD_DOMAIN:
        raise ValueError("HDFC Bank job detail company code or domain changed")
    if str(company.get("companyMstrSeq")) != COMPANY_SEQ:
        raise ValueError("HDFC Bank job detail company sequence changed")
    identity = str(job.get("jobSeq") or "")
    expected_identity = str(listing_row.get("jobId"))
    if identity != expected_identity:
        raise ValueError(f"HDFC Bank job detail identity mismatch: {identity} != {expected_identity}")
    if value.get("linkDeactivated") is True:
        raise ValueError(f"HDFC Bank job detail {identity} is deactivated")
    if job.get("jobStatus") != "ACTIVE":
        raise ValueError(f"HDFC Bank job detail {identity} is not ACTIVE")
    if normalized(job.get("jobTitle")) != normalized(listing_row.get("jobTitle")):
        raise ValueError(f"HDFC Bank job detail title mismatch for {identity}")
    if value.get("jobCount") is not None and not isinstance(value.get("jobCount"), int):
        raise ValueError("HDFC Bank job detail jobCount schema changed")
    description = job.get("jobDesc")
    if description is not None and not isinstance(description, str):
        raise ValueError("HDFC Bank job detail jobDesc is not HTML text")
    description = description or ""
    sections = description_sections(description)
    body = text_html(description)
    issues = []
    if not body.strip():
        issues.append("hdfc_job_detail_description_empty")
    elif len(body.strip()) < 100:
        issues.append("hdfc_job_detail_description_shorter_than_100_chars")
    return {
        "company_vo": company,
        "job_vo": job,
        "description_html": description,
        "description_text": body,
        "description_sections": sections,
        "date_posted": parse_source_date(job.get("jobPostingDate")),
        "date_posted_raw": job.get("jobPostingDate"),
        "valid_through": parse_source_date(job.get("hiringDeadline")),
        "locations": locations_from_detail(job),
        "issues": issues,
    }


def _apply_url(listing_url: str, identity: str) -> str:
    return listing_url.split("#", 1)[0] + f"#apply/job/{identity}"


def _detail_url(token: str, identity: str) -> str:
    return f"{DETAIL_API}?token={token}&jobSeq={identity}&source={CAREER_SOURCE}&lang={LANG}"


class HDFCBank(Adapter):
    """Collect exactly the roles HDFC Bank publishes on its tokenised RippleHire board."""

    VERIFICATION_REVISION = "hdfc-bank.ripplehire-company-board.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url).rstrip("/") != OFFICIAL_FINAL_EXPECTED:
            raise ValueError("HDFC Bank official careers entry did not reach the inspected current URL")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(), response_bytes=len(response.content))
        return parsed

    async def _board_shell(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_LINK)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_shell(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(), response_bytes=len(response.content))
        return parsed

    async def _listing_snapshot(self, name: str, token: str, expected_total: int | None = None):
        started = time.perf_counter()
        rows, ids, issues = [], [], []
        total = expected_total
        page = 0
        page_records = []
        while True:
            if total is not None and len(ids) >= total:
                break
            if not self.page_guard():
                issues.append("hdfc_listing_page_budget_exhausted")
                break
            request_body = {
                "page": page,
                "search": UNFILTERED_SEARCH,
                "token": token,
                "source": CAREER_SOURCE,
                "pagesize": PAGE_SIZE,
            }
            request_started = time.perf_counter()
            response = await self.http.request(
                "POST", SEARCH_API,
                data={"careerSiteUrlParams": json.dumps(request_body, separators=(",", ":")), "lang": LANG},
                headers={"Referer": f"{BOARD_ORIGIN}{LISTING_PATH}?token={token}&source={CAREER_SOURCE}#list"},
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed_url = urlsplit(str(response.url))
            if parsed_url.scheme != "https" or parsed_url.netloc != BOARD_DOMAIN or parsed_url.path != "/candidate/candidatejobsearch":
                raise ValueError("HDFC Bank listing API redirected outside the company board")
            try:
                page_value = parse_listing_page(response.json(), page, expected_total=total)
            except ValueError as exc:
                issues.append(f"hdfc_listing_page_schema_or_total_error:{exc}")
                break
            if total is None:
                total = page_value["total"]
            issues.extend(page_value["issues"])
            for row in page_value["rows"]:
                identity = str(row["jobId"])
                if identity in ids:
                    issues.append(f"hdfc_listing_duplicate_job_id_across_pages:{identity}")
                    continue
                ids.append(identity)
                rows.append(row)
            page_records.append({
                "page": page,
                "count": total,
                "start": page_value["start"],
                "page_size": PAGE_SIZE,
                "job_ids": page_value["ids"],
                "request_body": request_body,
                "request_seconds": round(request_seconds, 6),
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            page += 1
            if not page_value["rows"]:
                break
            if total is not None and len(ids) > total:
                issues.append("hdfc_listing_exceeded_declared_total")
                break
            if page > 1000:
                issues.append("hdfc_listing_page_cap_reached")
                break
        finished = time.perf_counter()
        return {
            "name": name,
            "rows": rows,
            "ids": ids,
            "total": total,
            "pages": page_records,
            "page_count": len(page_records),
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, finished - started),
        }

    async def _detail(self, job):
        identity = str(job.job_id)
        url = _detail_url(self.token, identity)
        response = await self.http.get(url)
        parsed = parse_job_detail(response.json(), job.raw["listing"])
        job.raw["detail"] = parsed["job_vo"]
        job.raw["detail_company"] = parsed["company_vo"]
        job.raw["detail_api_url"] = url
        job.raw["detail_api_response_sha256"] = sha256(response.content).hexdigest()
        job.raw["description_sections"] = parsed["description_sections"]
        job.raw["publish_details"] = parsed["job_vo"].get("publishDetails")
        job.description_html = parsed["description_html"]
        job.description_method = "company_scoped_ripplehire_jobdetail_api:jobDesc"
        job.description_source_url = url
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed["valid_through"]
        if parsed["locations"]:
            job.locations = parsed["locations"]
        detail_company = parsed["company_vo"]
        job.employer = normalized(detail_company.get("companyName")) or COMPANY_NAME
        job.department = parsed["job_vo"].get("bussinessUnit") or job.department
        job.employment_type = parsed["job_vo"].get("jobTypeCustom3") or job.employment_type
        job.salary = parsed["job_vo"].get("compensationInfo") or parsed["job_vo"].get("compensationRange")
        sections = parsed["description_sections"]
        job.responsibilities = _section_block(
            sections, r"responsibilit", r"education|qualification|key skills|certification|competenc")
        job.qualifications = _section_text(sections, r"qualification|competenc|key skills|certification")
        job.education = _section_text(sections, r"education")
        if parsed["issues"]:
            job.description_unavailable = "hdfc_job_detail_description_empty" in parsed["issues"]
            job.warnings.extend(parsed["issues"])
        detail_title = (parsed["job_vo"].get("jobTitle") or "").strip()
        if normalized(detail_title) == normalized(job.title):
            # Keep the detail page's exact published spelling, including internal spacing.
            job.title = detail_title or job.title
        else:
            job.warnings.append("hdfc_listing_detail_title_mismatch")
        return job

    async def run(self):
        started = time.perf_counter()
        entry = await self._official_entry()
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        board = await self._board_shell()
        self.result.timings["board_redirect_seconds"] = board["seconds"]
        self.token = board["token"]
        self.listing_url = board["listing_url"]

        initial = await self._listing_snapshot("initial", self.token)
        self.result.timings["initial_listing_seconds"] = initial["seconds"]
        self.result.expected_total = initial["total"]
        for issue in initial["issues"]:
            self.result.errors.append(issue)
        if initial["total"] is not None and len(initial["ids"]) != initial["total"]:
            self.result.errors.append("hdfc_initial_listing_membership_count_mismatch")

        base = self.listing_url.split("#", 1)[0]
        initial_by_id = {str(row["jobId"]): row for row in initial["rows"]}
        for row in initial["rows"]:
            identity = str(row["jobId"])
            detail_url = _detail_url(self.token, identity)
            self.add(self.job(
                {"listing": row, "listing_api_url": SEARCH_API + "?pagesize=" + str(PAGE_SIZE) + "&page=0"},
                title=(row.get("jobTitle") or "").strip(),
                job_id=identity,
                url=f"{base}#detail/job/{identity}",
                apply_url=_apply_url(self.listing_url, identity),
                description="",
                method=None,
                description_url=detail_url,
                locations=locations_from_detail(row),
                department=row.get("bussinessUnit"),
                employment_type=row.get("jobTypeCustom3"),
                date_posted_raw=row.get("jobPostingDate"),
                employer=COMPANY_NAME,
                language=LANG,
            ))
            if self.limit():
                break

        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        if "sample_limit" not in self.result.warnings:
            final = await self._listing_snapshot("final", self.token)
            self.result.timings["final_listing_seconds"] = final["seconds"]
            for issue in final["issues"]:
                self.result.errors.append(issue)
            if final["total"] != initial["total"]:
                self.result.errors.append("hdfc_final_listing_declared_total_changed")
            final_by_id = {str(row["jobId"]): row for row in final["rows"]}
            self.result.metadata["initial_listing_ids"] = initial["ids"]
            self.result.metadata["final_listing_ids"] = final["ids"]
            if final["ids"] != initial["ids"] or final_by_id != initial_by_id:
                self.result.errors.append("hdfc_initial_final_listing_membership_or_fields_changed")
            if len(final["rows"]) != initial["total"]:
                self.result.errors.append("hdfc_final_listing_count_mismatch")
        else:
            final = None
            self.result.metadata["initial_listing_ids"] = initial["ids"]
            self.result.metadata["final_listing_ids"] = None

        self.result.metadata.update({
            "official_entry": entry,
            "board_shell": board,
            "listing_endpoint": SEARCH_API,
            "detail_endpoint": DETAIL_API,
            "page_size": PAGE_SIZE,
            "observed_listing_total": initial["total"],
            "observed_listing_pages": initial["page_count"],
            "initial_listing": {
                "ids": initial["ids"],
                "pages": initial["pages"],
                "issues": initial["issues"],
            },
            "final_listing": None if final is None else {
                "ids": final["ids"],
                "pages": final["pages"],
                "issues": final["issues"],
            },
            "membership_reconciliation": {
                "complete": bool(
                    "sample_limit" not in self.result.warnings
                    and initial["total"] is not None
                    and len(initial["ids"]) == initial["total"]
                    and len(set(initial["ids"])) == initial["total"]
                    and final is not None
                    and final["ids"] == initial["ids"]
                ),
                "initial_count": len(initial["ids"]),
                "final_count": None if final is None else len(final["ids"]),
                "declared_total": initial["total"],
                "initial_ids": initial["ids"],
                "final_ids": None if final is None else final["ids"],
            },
            "representative_detail_refs": {
                "first": initial["ids"][0] if initial["ids"] else None,
                "last": initial["ids"][-1] if initial["ids"] else None,
                "first_url": self.result.jobs[0].url if self.result.jobs else None,
                "last_url": next((job.url for job in self.result.jobs
                                  if initial["ids"] and job.job_id == initial["ids"][-1]), None),
                "independent_rendered_reference": "companies/hdfc-bank/browser-reference-worker.json",
            },
            "request_counts": {
                "official_entry": 1,
                "board_redirect": 1,
                "listing_pages_initial": initial["page_count"],
                "listing_pages_final": None if final is None else final["page_count"],
                "job_details": len(self.result.jobs),
            },
            "apply_route": "https://hdfcbank.ripplehire.com/candidate/?token=...#apply/job/<jobSeq>",
        })
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.listing_complete = bool(
            "sample_limit" not in self.result.warnings
            and not any(error.startswith("hdfc_") for error in self.result.errors)
            and initial["total"] is not None
            and len(initial["ids"]) == initial["total"]
        )
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        return self.result
