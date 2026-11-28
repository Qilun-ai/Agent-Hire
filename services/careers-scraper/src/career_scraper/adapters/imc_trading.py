"""IMC Trading's own Next.js careers search and its company-scoped Greenhouse job pages."""
from __future__ import annotations

from hashlib import sha256
import html as html_module
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


LEDGER_ENTRY = "https://www.imc.com/us/careers/jobs"
OFFICIAL_ENTRY = "https://www.imc.com/us/search-careers"
GLOBAL_LISTING_URL = "https://www.imc.com/eu/search-careers"
JOBS_URL = "https://www.imc.com/{region}/careers/jobs/{job_id}"
REGION_LISTINGS = {
    "eu": (GLOBAL_LISTING_URL, None),
    "us": ("https://www.imc.com/us/search-careers", "United States"),
    "ap": ("https://www.imc.com/ap/search-careers", "Australia"),
    "hk": ("https://www.imc.com/hk/search-careers", "Hong Kong"),
    "in": ("https://www.imc.com/in/search-careers", "India"),
}
JOB_PATH = re.compile(r"^/([a-z]{2})/careers/jobs/(\d+)$")
GREENHOUSE_JOB_HOSTS = {"job-boards.eu.greenhouse.io", "job-boards.greenhouse.io"}
SALARY_LINE = re.compile(r"^(?:base salary|salary range|salary|compensation)\b", re.I)
SALARY_LABEL_ONLY = re.compile(r"^(?:base salary|salary range|salary)\s*:?\s*$", re.I)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def decode_rsc_streams(html: str) -> list[str]:
    """Decode every RSC text payload embedded by IMC's Next.js page."""
    soup = BeautifulSoup(html, "lxml")
    streams: list[str] = []
    for script in soup.find_all("script"):
        text = script.string or ""
        if "self.__next_f.push" not in text:
            continue
        for match in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', text, re.S):
            streams.append(json.loads(match.group(1)))
    if not streams:
        raise ValueError("IMC page no longer contains decodable Next.js RSC data")
    return streams


def _balanced_value(text: str, start: int) -> str:
    opener = text[start]
    if opener not in "[{":
        raise ValueError("Expected a JSON array or object")
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("Unbalanced JSON value in IMC RSC data")


def _find_jobs_array(streams: list[str]) -> list[dict]:
    for stream in streams:
        for match in re.finditer(r'"jobs":\s*\[', stream):
            start = stream.find("[", match.start())
            try:
                value = json.loads(_balanced_value(stream, start))
            except (ValueError, json.JSONDecodeError):
                continue
            if (isinstance(value, list) and value and len(value) > 2
                    and all(isinstance(row, dict) and "id" in row and "title" in row for row in value[:3])):
                return value
    raise ValueError("IMC listing no longer exposes its JobsProvider jobs array")


def _find_job_object(streams: list[str]) -> dict:
    for stream in streams:
        for match in re.finditer(r'"job":\s*\{', stream):
            start = stream.find("{", match.start())
            try:
                value = json.loads(_balanced_value(stream, start))
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and "id" in value and "title" in value and "content" in value:
                return value
    raise ValueError("IMC detail page no longer exposes its embedded Greenhouse job object")


def _find_text_chunk(streams: list[str], reference: object) -> str | None:
    """Follow a detail page's RSC content reference such as `$5e`."""
    if not isinstance(reference, str):
        return None
    if not reference.startswith("$"):
        return reference
    line = reference[1:]
    if not re.fullmatch(r"[0-9a-fA-F]+", line):
        return None
    pattern = re.compile(rf"(?m)^{re.escape(line)}:T([0-9a-fA-F]+),")
    # RSC streaming may split a T chunk between two self.__next_f pushes; the
    # decoded pushes are consecutive pieces of one logical stream.
    stream = "".join(streams)
    match = pattern.search(stream)
    if match:
        start = match.end()
        length = int(match.group(1), 16)
        # RSC T-chunk lengths are UTF-8 byte lengths, not Python character counts.
        start_byte = len(stream[:start].encode("utf-8"))
        raw = stream.encode("utf-8")[start_byte:start_byte + length].decode("utf-8")
        return html_module.unescape(raw)
    return None


def _meta(job: dict, name: str):
    for item in job.get("metadata") or []:
        if item.get("name") == name:
            return item.get("value")
    return None


def _visible_counts(html: str) -> tuple[int | None, int | None]:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    match = re.search(r"Showing\s+(\d+)\s+of\s+(\d+)\s+jobs", text, re.I)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def parse_listing(html: str, response_url: str = GLOBAL_LISTING_URL) -> dict:
    """Parse one official IMC region search page and its embedded global job rows."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.imc.com":
        raise ValueError("IMC listing response is not on www.imc.com")
    if not re.fullmatch(r"/[a-z]{2}/search-careers", parts.path):
        raise ValueError("IMC listing response is not a search-careers route")
    if parts.query:
        raise ValueError("IMC listing response unexpectedly carries a query string")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not page_title.startswith("Search careers"):
        raise ValueError(f"Unexpected IMC listing title: {page_title!r}")
    rows = _find_jobs_array(decode_rsc_streams(html))
    hidden = [row for row in rows if _meta(row, "Is Hidden Job?") is True]
    public = [row for row in rows if _meta(row, "Is Hidden Job?") is not True]
    visible, advertised = _visible_counts(html)
    region = parts.path.strip("/").split("/")[0]
    issues = []
    if not public:
        issues.append("imc_company_listing_has_no_public_rows")
    if visible is None or advertised is None:
        issues.append("imc_visible_job_count_missing")
    if len(public) + len(hidden) != len(rows):
        issues.append("imc_hidden_job_flags_inconsistent")
    return {
        "url": response_url,
        "region": region,
        "page_title": page_title,
        "rows": public,
        "hidden_rows": hidden,
        "embedded_total": len(rows),
        "public_total": len(public),
        "hidden_total": len(hidden),
        "visible_count": visible,
        "advertised_total": advertised,
        "issues": issues,
    }


def count_office(rows: list[dict], country: str) -> int:
    needle = country.lower()
    return sum(
        any(needle in (office.get("location") or "").lower()
            for office in row.get("offices") or [] if isinstance(office, dict))
        for row in rows
    )


def description_sections(content: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-imc-body="true">{content or ""}</div>', "lxml")
    root = soup.select_one("[data-imc-body]")
    if root is None:
        return []
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    sections: list[dict] = []
    current = None
    for block in [node for node in root.contents if isinstance(node, Tag)]:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _salary_text_lines(body_text: str) -> list[str]:
    """Keep explicit salary labels, joining a bare label to its next money line."""
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    found: list[str] = []
    for index, line in enumerate(lines):
        if not SALARY_LINE.match(line):
            continue
        value = line
        if (SALARY_LABEL_ONLY.match(line) and index + 1 < len(lines)
                and re.search(r"\d", lines[index + 1])):
            value = f"{line} {lines[index + 1]}"
        found.append(value)
    return list(dict.fromkeys(found))


def parse_detail(html: str, expected_row: dict, response_url: str) -> dict:
    """Parse the full employer description and public fields from an IMC job page."""
    parts = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc != "www.imc.com" or match is None:
        raise ValueError("IMC detail response is not an exact company job route")
    streams = decode_rsc_streams(html)
    job = _find_job_object(streams)
    identity = str(job.get("id"))
    if identity != str(expected_row.get("id")) or match.group(2) != identity:
        raise ValueError("IMC detail page job id does not match the listing row")
    expected_title = normalized_title(expected_row.get("title"))
    actual_title = normalized_title(job.get("title"))
    if not actual_title or actual_title != expected_title:
        raise ValueError(f"IMC detail title changed: {actual_title!r} != {expected_title!r}")
    if job.get("company_name") not in ("IMC", "IMC Trading"):
        raise ValueError(f"Unexpected IMC detail employer: {job.get('company_name')!r}")
    absolute_url = urlsplit(job.get("absolute_url") or "")
    if (absolute_url.scheme != "https" or absolute_url.netloc not in GREENHOUSE_JOB_HOSTS
            or not re.fullmatch(rf"/imc/jobs/{identity}", absolute_url.path)):
        raise ValueError("IMC detail is not linked to its company-scoped Greenhouse job")
    content = _find_text_chunk(streams, job.get("content"))
    if not content or len(text_html(content)) < 100:
        raise ValueError("IMC detail full description is missing or truncated")
    offices = job.get("offices") or []
    locations = list(dict.fromkeys(
        office.get("location") for office in offices
        if isinstance(office, dict) and office.get("location")
    ))
    departments = job.get("departments") or []
    department = departments[0].get("name") if departments and isinstance(departments[0], dict) else None
    sections = description_sections(content)
    responsibilities = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                     for token in ("responsibilit", "what you will do", "your role"))
    ]
    qualifications = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                      for token in ("skill", "qualif", "requirement",
                                                    "who you are", "what you'll need", "experience"))
    ]
    body_text = text_html(content)
    salary_lines = _salary_text_lines(body_text)
    salary = job.get("pay_input_ranges") or (
        {"text": salary_lines, "source": "description_text"} if salary_lines else None
    )
    return {
        "job": job,
        "content": content,
        "description_text": body_text,
        "title": actual_title,
        "locations": locations,
        "department": department,
        "employment_type": _meta(job, "Worker Sub Type"),
        "job_category": _meta(job, "Job Category"),
        "career_site_locale": _meta(job, "Career Site Locale"),
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "salary": salary,
        "dates": {
            "first_published": job.get("first_published"),
            "updated_at": job.get("updated_at"),
            "application_deadline": job.get("application_deadline"),
        },
        "requisition_id": job.get("requisition_id"),
        "absolute_url": job.get("absolute_url"),
        "internal_job_id": job.get("internal_job_id"),
        "company_name": job.get("company_name"),
        "language": job.get("language"),
    }


class ImcTrading(Adapter):
    """Collect IMC's own published search listing and exact company job pages."""

    VERIFICATION_REVISION = "imc-trading.nextjs-rsc-greenhouse.1"

    def _listing_job(self, row: dict, region: str, listing_url: str):
        identity = str(row["id"])
        url = JOBS_URL.format(region=region, job_id=identity)
        offices = [office for office in row.get("offices") or [] if isinstance(office, dict)]
        locations = list(dict.fromkeys(office.get("location") for office in offices if office.get("location")))
        departments = [dept for dept in row.get("departments") or [] if isinstance(dept, dict)]
        department = departments[0].get("name") if departments else None
        return self.job(
            {
                "listing": dict(row),
                "listing_url": listing_url,
                "listing_region": region,
                "application_route": {
                    "official_detail_url": url,
                    "official_apply_url": url + "/apply",
                },
            },
            title=normalized_title(row.get("title")),
            url=url,
            job_id=identity,
            apply_url=url + "/apply",
            locations=locations,
            department=department,
            employment_type=_meta(row, "Worker Sub Type"),
            date_updated=row.get("updated_at"),
            date_posted_raw={"listing_updated_at": row.get("updated_at")},
        )

    async def _listing_snapshot(self, region: str, url: str, country: str | None):
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"IMC {region} listing redirected away from its inspected route: {response.url}")
        parsed = parse_listing(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "request_seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "office_country": country,
        })
        return parsed

    async def _fetch_detail(self, listing_job):
        started = time.perf_counter()
        response = await self.http.get(listing_job.url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != listing_job.url:
            raise ValueError(f"IMC job detail redirected away from its inspected route: {response.url}")
        parse_started = time.perf_counter()
        parsed = parse_detail(response.text, listing_job.raw["listing"], str(response.url))
        self.result.timings["detail_parse_seconds"] = (
            self.result.timings.get("detail_parse_seconds", 0.0)
            + max(0.0, time.perf_counter() - parse_started)
        )
        identity = str(listing_job.job_id)
        detail = parsed["job"]
        raw_detail = {
            key: value for key, value in detail.items()
            if key not in {"content", "questions", "demographic_questions", "location_questions", "data_compliance"}
        }
        raw_detail["application_question_count"] = len(detail.get("questions") or [])
        raw_detail["demographic_question_count"] = len(
            (detail.get("demographic_questions") or {}).get("questions") or []
        )
        raw_detail["content_ref"] = detail.get("content")
        raw_detail["content_sha256"] = sha256(parsed["content"].encode()).hexdigest()
        raw_detail["content_normalized_chars"] = len(parsed["description_text"])
        raw_detail["description_sections"] = parsed["sections"]
        raw_detail["salary_text_lines"] = _salary_text_lines(parsed["description_text"])
        raw_detail["application_route"] = {
            "official_detail_url": listing_job.url,
            "official_apply_url": listing_job.url + "/apply",
            "greenhouse_exact_job_url": parsed["absolute_url"],
            "apply_form_questions_excluded_from_description": True,
        }
        raw_detail["detail_response"] = {
            "url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "request_seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        job = self.job(
            {"listing": listing_job.raw["listing"], "listing_url": listing_job.raw["listing_url"],
             "listing_region": listing_job.raw["listing_region"], "detail": raw_detail},
            title=parsed["title"],
            url=listing_job.url,
            job_id=identity,
            requisition_id=parsed["requisition_id"],
            apply_url=listing_job.url + "/apply",
            description=parsed["content"],
            method="imc.nextjs.rsc.greenhouse_job.content",
            description_url=listing_job.url,
            locations=parsed["locations"],
            department=parsed["department"],
            employment_type=parsed["employment_type"],
            workplace_type=None,
            date_posted=parsed["dates"]["first_published"],
            date_posted_raw=parsed["dates"],
            date_updated=parsed["dates"]["updated_at"],
            valid_through=parsed["dates"]["application_deadline"],
            salary=parsed["salary"],
            qualifications=parsed["qualifications"],
            responsibilities=parsed["responsibilities"],
            employer=parsed["company_name"],
            language=parsed["language"],
        )
        job.description_text = parsed["description_text"]
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "ledger_entry_url": LEDGER_ENTRY,
            "ledger_entry_observed_status": "404 (route retired; /careers redirects to /us/search-careers)",
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": GLOBAL_LISTING_URL,
            "listing_authority": "IMC's own search-careers page. The Europe route is the unfiltered global listing; US/AP/HK/IN are its country-filtered views.",
            "listing_request": {
                "method": "GET",
                "routes": {region: url for region, (url, _) in REGION_LISTINGS.items()},
                "query_shape": "page state is client-side; ?page=N is applied after hydration, not by the server",
                "filters_applied": False,
            },
            "detail_method": "Exact IMC /<region>/careers/jobs/<id> page; full employer HTML is the RSC Greenhouse job.content value.",
            "request_scope": "www.imc.com only for listing/detail membership; the only ATS reference is the exact company-scoped job-boards.eu.greenhouse.io/imc job URL embedded in each detail.",
        })
        try:
            snapshots = {}
            for region, (url, country) in REGION_LISTINGS.items():
                snapshot_started = time.perf_counter()
                snapshots[region] = await self._listing_snapshot(region, url, country)
                self.result.timings[f"listing_{region}_seconds"] = max(
                    0.0, time.perf_counter() - snapshot_started
                )
        except Exception as exc:
            self.result.errors.append(f"IMC source listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        global_listing = snapshots["eu"]
        rows = global_listing["rows"]
        expected_total = global_listing["advertised_total"]
        self.result.expected_total = expected_total
        for snapshot in snapshots.values():
            self.result.errors.extend(snapshot["issues"])
        if expected_total is None or len(rows) != expected_total:
            self.result.errors.append("imc_global_listing_total_does_not_match_nonhidden_rows")
        global_ids = [str(row["id"]) for row in rows]
        for region, snapshot in snapshots.items():
            region_ids = [str(row["id"]) for row in snapshot["rows"]]
            if region_ids != global_ids:
                self.result.errors.append(f"imc_region_embedded_rows_differ_from_global:{region}")
            country = snapshot["office_country"]
            computed = count_office(rows, country) if country else len(rows)
            if snapshot["advertised_total"] != computed:
                self.result.errors.append(
                    f"imc_region_listing_count_mismatch:{region}:"
                    f"advertised={snapshot['advertised_total']}:computed={computed}"
                )
        self.result.metadata["region_listing_reconciliation"] = {
            region: {
                **{key: snapshot[key] for key in (
                    "url", "status", "page_title", "embedded_total", "public_total", "hidden_total",
                    "visible_count", "advertised_total", "office_country", "request_seconds",
                    "response_bytes", "response_sha256", "issues",
                )},
                "computed_rows_for_region": count_office(rows, snapshot["office_country"]) if snapshot["office_country"] else len(rows),
                "first_ids": [str(row["id"]) for row in snapshot["rows"][:3]],
                "last_ids": [str(row["id"]) for row in snapshot["rows"][-3:]],
            }
            for region, snapshot in snapshots.items()
        }
        self.result.metadata["global_listing"] = {
            "url": GLOBAL_LISTING_URL,
            "embedded_total_including_hidden": global_listing["embedded_total"],
            "public_total": global_listing["public_total"],
            "hidden_total": global_listing["hidden_total"],
            "advertised_total": expected_total,
            "visible_first_page": global_listing["visible_count"],
            "first_ids": global_ids[:3],
            "last_ids": global_ids[-3:],
            "hidden_rows": [
                {
                    "id": str(row.get("id")),
                    "title": row.get("title"),
                    "offices": [office.get("location") for office in row.get("offices") or [] if isinstance(office, dict)],
                    "reason": "Greenhouse metadata Is Hidden Job? = true; absent from every regional search listing and its advertised count.",
                }
                for row in global_listing["hidden_rows"]
            ],
        }

        jobs = [self._listing_job(row, "eu", GLOBAL_LISTING_URL) for row in rows]
        for job in jobs:
            self.add(job)
        if len(self.result.jobs) != len(rows):
            self.result.errors.append("imc_listing_rows_duplicated_or_lost_before_details")

        self.result.metadata["listing_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        detail_started = time.perf_counter()
        hydrated = await self.hydrate(self.result.jobs, self._fetch_detail)
        self.result.jobs = hydrated
        self.result.timings["detail_wall_seconds"] = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        try:
            final_snapshot = await self._listing_snapshot(
                "eu", GLOBAL_LISTING_URL, REGION_LISTINGS["eu"][1]
            )
            final_ids = [str(row["id"]) for row in final_snapshot["rows"]]
            self.result.metadata["final_listing_recheck"] = {
                "url": final_snapshot["url"],
                "status": final_snapshot["status"],
                "advertised_total": final_snapshot["advertised_total"],
                "public_total": final_snapshot["public_total"],
                "first_ids": final_ids[:3],
                "last_ids": final_ids[-3:],
                "ids_match_initial": final_ids == global_ids,
                "request_seconds": final_snapshot["request_seconds"],
            }
            if final_ids != global_ids or final_snapshot["advertised_total"] != expected_total:
                self.result.errors.append("imc_final_listing_membership_changed")
        except Exception as exc:
            self.result.errors.append(f"IMC final listing recheck failed: {type(exc).__name__}: {exc}")
            self.result.metadata["final_listing_recheck"] = {"error": f"{type(exc).__name__}: {exc}"}
        self.result.timings["final_listing_recheck_seconds"] = max(
            0.0, time.perf_counter() - final_started
        )

        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.metadata["collection_summary"] = {
            "advertised_total": expected_total,
            "jobs_retained": len(self.result.jobs),
            "descriptions_verified": verified,
            "descriptions_unverified_ids": [
                str(job.job_id) for job in self.result.jobs if not job.description_verified
            ],
            "first_id": str(self.result.jobs[0].job_id) if self.result.jobs else None,
            "last_id": str(self.result.jobs[-1].job_id) if self.result.jobs else None,
        }
        self.result.listing_complete = (
            len(self.result.jobs) == expected_total
            and not self.result.errors
            and self.result.metadata.get("final_listing_recheck", {}).get("ids_match_initial") is True
        )
        self.result.metadata["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
