"""JUSPAY's own careers page: the published global listing and its exact job pages."""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://juspay.in/careers"
CAREERS_URL = "https://juspay.io/careers"
JOBS_API = "https://joinus.juspay.in/api/careerJobOpening"
LISTING_API_URL = JOBS_API + "?limit=1000&isGlobal=true"
DETAIL_URL = "https://juspay.io/careers/{job_id}"
APPLY_URL = "https://joinus.juspay.in?jobId={job_id}"
APPLY_HOST = "joinus.juspay.in"
EMPLOYER = "Juspay Technologies Pvt. Ltd"
PAGE_TITLE = "Juspay | Explore career opportunities in payments tech"
LISTING_COMPONENT = re.compile(r"^/_astro/JobListings\.[A-Za-z0-9_-]+\.js$")
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_HEADINGS = ("responsibilit", "what you will do", "what you'll do", "your role")
RESPONSIBILITY_STOP_HEADINGS = ("requirement", "qualif", "essential", "desirable", "why join",
                                "perks", "benefits")
QUALIFICATION_HEADINGS = ("requirement", "qualif", "essential", "what we're looking for",
                          "what you'll need", "who you are", "skills")
QUALIFICATION_STOP_HEADINGS = ("why join", "perks", "benefits")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def normalize_for_compare(value: str | None) -> str:
    """Smart quotes/dashes are presentation; the employer-authored words must match."""
    value = unicodedata.normalize("NFKC", value or "")
    value = (value.replace("\u2019", "'").replace("\u2018", "'")
             .replace("\u201c", '"').replace("\u201d", '"')
             .replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " "))
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def markdown_to_text(value: str | None) -> str:
    """Plain text of the API markdown body, for comparison with the rendered page."""
    text = value or ""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def decode_devalue(value):
    """Decode the small Astro/devalue subset used by the SSR job island props."""
    if isinstance(value, list):
        if len(value) == 2 and value[0] == 0:
            return decode_devalue(value[1])
        if len(value) == 2 and value[0] == 1:
            return [decode_devalue(item) for item in value[1]]
        return [decode_devalue(item) for item in value]
    if isinstance(value, dict):
        return {key: decode_devalue(item) for key, item in value.items()}
    return value


def _validate_listing_row(row: dict, issues: list[str], source: str) -> bool:
    identity = row.get("job_id")
    if not isinstance(identity, str) or not JOB_ID.fullmatch(identity):
        issues.append(f"{source}_row_missing_job_id")
        return False
    if row.get("is_global") is not True:
        issues.append(f"{source}_row_not_global:{identity}")
        return False
    if row.get("opening_status") is not True:
        issues.append(f"{source}_row_not_open:{identity}")
        return False
    for field in ("job_title", "job_location", "job_type", "category", "job_description_career"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            issues.append(f"{source}_row_missing_{field}:{identity}")
            return False
    body = normalize_for_compare(markdown_to_text(row["job_description_career"]))
    if len(body) < 100:
        issues.append(f"{source}_description_empty_or_short:{identity}")
        return False
    return True


def parse_official_entry(html: str, response_url: str = CAREERS_URL) -> dict:
    """Read the SSR job island published on JUSPAY's own /careers page."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "juspay.io" or parsed.path.rstrip("/") != "/careers":
        raise ValueError("Juspay official careers entry did not land on its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != PAGE_TITLE:
        raise ValueError("Juspay official careers page title changed")
    islands = [
        node for node in soup.select("astro-island[component-url]")
        if LISTING_COMPONENT.fullmatch(node.get("component-url", ""))
    ]
    if len(islands) != 1:
        raise ValueError("Juspay official careers page no longer exposes one JobListings island")
    island = islands[0]
    raw_props = island.get("props")
    if not raw_props:
        raise ValueError("Juspay JobListings island has no SSR props")
    try:
        props = json.loads(raw_props)
    except json.JSONDecodeError:
        try:
            props = json.loads(unescape(raw_props))
        except json.JSONDecodeError as exc:
            raise ValueError("Juspay JobListings SSR props are not valid JSON") from exc
    job_data = decode_devalue(props.get("jobData"))
    if not isinstance(job_data, list) or not job_data:
        raise ValueError("Juspay JobListings SSR props have no jobData rows")
    rows, issues, seen = [], [], set()
    for row in job_data:
        if not isinstance(row, dict):
            issues.append("juspay_ssr_row_not_object")
            continue
        if not _validate_listing_row(row, issues, "juspay_ssr"):
            continue
        identity = row["job_id"]
        if identity in seen:
            issues.append(f"juspay_ssr_duplicate_job_id:{identity}")
            continue
        seen.add(identity)
        rows.append(dict(row))
    if not rows:
        raise ValueError("Juspay JobListings SSR props contain no usable global open job rows")
    return {
        "url": response_url,
        "page_title": title,
        "island_component_url": island.get("component-url"),
        "rows": rows,
        "job_ids": [row["job_id"] for row in rows],
        "count": len(rows),
        "issues": issues,
    }


def parse_listing_api(value: dict, response_url: str = LISTING_API_URL) -> dict:
    """Read the exact request the official page's JobListings component revalidates."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != APPLY_HOST
            or parsed.path != "/api/careerJobOpening"
            or parse_qs(parsed.query) != {"limit": ["1000"], "isGlobal": ["true"]}):
        raise ValueError("Juspay listing API response came from an unexpected route")
    if not isinstance(value, dict):
        raise ValueError("Juspay listing API response is not an object")
    summary = value.get("summary")
    all_jobs = value.get("allJobs")
    if not isinstance(summary, dict) or not isinstance(all_jobs, list):
        raise ValueError("Juspay listing API response is missing summary or allJobs")
    count = summary.get("count")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Juspay listing API summary.count is missing")
    rows, issues, seen = [], [], set()
    for row in all_jobs:
        if not isinstance(row, dict):
            issues.append("juspay_api_row_not_object")
            continue
        if not _validate_listing_row(row, issues, "juspay_api"):
            continue
        identity = row["job_id"]
        if identity in seen:
            issues.append(f"juspay_api_duplicate_job_id:{identity}")
            continue
        seen.add(identity)
        rows.append(dict(row))
    if count != len(all_jobs):
        issues.append("juspay_api_summary_count_does_not_match_rows")
    if len(rows) != count:
        issues.append("juspay_api_usable_rows_do_not_match_summary_count")
    return {
        "url": response_url,
        "rows": rows,
        "job_ids": [row["job_id"] for row in rows],
        "count": count,
        "summary": dict(summary),
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-juspay-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-juspay-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
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
        current["text"] = normalized_body_text(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = normalized_body_text(section["html"])
    return sections


def _sections_between(sections: list[dict], start_tokens: tuple[str, ...],
                      stop_tokens: tuple[str, ...]) -> list[dict] | None:
    collected, active = [], False
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if not active and any(token in heading for token in start_tokens):
            active = True
        if active and collected and any(token in heading for token in stop_tokens):
            break
        if active:
            collected.append({"heading": section.get("heading"), "text": section.get("text", "")})
    return collected or None


def parse_detail_page(html: str, expected_row: dict, expected_url: str) -> dict:
    """Check the exact company detail page against its published listing row."""
    identity = expected_row.get("job_id")
    title = normalized_title(expected_row.get("job_title"))
    if not isinstance(identity, str) or expected_url != DETAIL_URL.format(job_id=identity):
        raise ValueError("Juspay detail URL is not the exact company-exposed job route")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if page_title != f"Juspay Careers | {title}":
        raise ValueError("Juspay detail page title does not match the listing title")
    heading = soup.find("h1")
    heading_text = normalized_title(heading.get_text(" ", strip=True)) if heading else ""
    if heading_text != title:
        raise ValueError("Juspay detail page h1 does not match the listing title")
    prose = soup.select_one("div.prose")
    if prose is None:
        raise ValueError("Juspay detail page has no full description body")
    description_html = prose.decode_contents()
    description_text = normalized_body_text(description_html)
    if len(description_text) < 100:
        raise ValueError("Juspay detail page description body is empty or truncated")

    apply_links = []
    for anchor in soup.select("a[href]"):
        target = urlsplit(urljoin(expected_url, anchor.get("href", "")))
        if (target.scheme == "https" and target.netloc == APPLY_HOST
                and parse_qs(target.query).get("jobId") == [identity]):
            apply_links.append({
                "href": anchor.get("href"),
                "resolved": target.geturl(),
                "text": normalized_title(anchor.get_text(" ", strip=True)),
            })
    if len(apply_links) != 1:
        raise ValueError("Juspay detail page no longer exposes exactly one job-scoped apply link")
    apply_link = apply_links[0]
    if apply_link["text"] != "Apply Now":
        raise ValueError("Juspay detail page apply control label changed")

    api_text = normalize_for_compare(markdown_to_text(expected_row.get("job_description_career")))
    page_text = normalize_for_compare(description_text)
    links = [{"text": a.get_text(" ", strip=True), "url": urljoin(expected_url, a.get("href", ""))}
             for a in prose.select("a[href]")]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    sections = description_sections(description_html)
    location_paragraph = next(
        (normalized_title(paragraph.get_text(" ", strip=True)) for paragraph in prose.find_all("p")
         if normalized_title(paragraph.get_text(" ", strip=True)).lower().startswith("location:")),
        None,
    )
    description_location = location_paragraph.split(":", 1)[1].strip() if location_paragraph else None
    location_cities = []
    if description_location:
        place = description_location.split("(", 1)[0]
        location_cities = [normalized_title(city) for city in re.split(r"[/,]", place) if normalized_title(city)]
    workplace_type = None
    if description_location:
        lowered = description_location.lower()
        workplace_type = ("Hybrid" if "hybrid" in lowered
                          else "Remote" if "remote" in lowered
                          else "On-site" if "on-site" in lowered or "onsite" in lowered else None)
    return {
        "url": expected_url,
        "title": title,
        "page_title": page_title,
        "h1": heading_text,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_sections": sections,
        "description_links": links,
        "role_attachments": attachments,
        "description_location": description_location,
        "description_location_cities": location_cities,
        "workplace_type": workplace_type,
        "workplace_type_source": "description Location line" if workplace_type else None,
        "apply_url": apply_link["href"],
        "apply_url_resolved": apply_link["resolved"],
        "apply_controls": [apply_link["text"]],
        "api_description_normalized_chars": len(api_text),
        "api_description_fnv1a64": fnv1a64(api_text),
        "matches_api_description_text": page_text == api_text,
        "responsibilities": _sections_between(sections, RESPONSIBILITY_HEADINGS, RESPONSIBILITY_STOP_HEADINGS),
        "qualifications": _sections_between(sections, QUALIFICATION_HEADINGS, QUALIFICATION_STOP_HEADINGS),
        "application_questions_excluded": True,
    }


class Juspay(Adapter):
    """Read only JUSPAY's own /careers listing and the exact job pages it exposes."""

    VERIFICATION_REVISION = "juspay.official-careers-ssr-api.1"

    async def _entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        redirects = [str(item.url) for item in response.history]
        if str(response.url) != CAREERS_URL:
            raise ValueError("Juspay careers entry redirected away from the inspected official URL")
        if redirects != [OFFICIAL_ENTRY]:
            raise ValueError(f"Juspay careers entry redirect chain changed: {redirects}")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "request_url": OFFICIAL_ENTRY,
            "final_url": str(response.url),
            "redirects": redirects,
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        return parsed

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_api(response.json(), str(response.url))
        parsed.update({
            "request_url": LISTING_API_URL,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        return parsed

    async def _detail(self, row, detail_url):
        started = time.perf_counter()
        response = await self.http.get(detail_url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != detail_url:
            raise ValueError(f"Juspay detail redirected away from the exposed job URL: {detail_url}")
        parsed = parse_detail_page(response.text, row, detail_url)
        parsed.update({
            "request_url": detail_url,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        return parsed

    def _make_job(self, row, detail):
        identity = row["job_id"]
        locations = [row["job_location"]]
        for city in detail["description_location_cities"]:
            if not any(city.lower() in existing.lower() for existing in locations):
                locations.append(city)
        job = self.job(
            {
                "listing": dict(row),
                "detail": {key: value for key, value in detail.items() if key != "description_html"},
            },
            title=detail["title"],
            url=detail["url"],
            job_id=identity,
            requisition_id=None,
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="juspay.careers.detail.div.prose",
            description_url=detail["url"],
            locations=locations,
            department=row["category"],
            employment_type=row["job_type"],
            workplace_type=detail["workplace_type"],
            experience=row["experience_year"],
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=EMPLOYER,
            language="en",
            responsibilities=detail["responsibilities"],
            qualifications=detail["qualifications"],
        )
        job.description_text = detail["description_text"]
        job.company_scope_verified = self.company.scope == "company"
        if not detail["matches_api_description_text"]:
            job.warnings.append("description_not_full_field")
            self.result.errors.append(f"juspay_detail_api_description_mismatch:{identity}")
        if detail["role_attachments"]:
            self.result.errors.append(f"juspay_role_attachments_require_exact_review:{identity}")
        return job

    def _membership(self, entry, listing):
        issues = list(entry.get("issues", [])) + list(listing.get("issues", []))
        same_ids = entry["job_ids"] == listing["job_ids"]
        entry_rows = {row["job_id"]: row for row in entry["rows"]}
        listing_rows = {row["job_id"]: row for row in listing["rows"]}
        changed = {}
        for identity in entry["job_ids"]:
            if identity in listing_rows and entry_rows[identity] != listing_rows[identity]:
                changed[identity] = sorted(
                    key for key in set(entry_rows[identity]) | set(listing_rows[identity])
                    if entry_rows[identity].get(key) != listing_rows[identity].get(key)
                )
        if not same_ids:
            issues.append("juspay_ssr_and_api_membership_mismatch")
        if changed:
            issues.append("juspay_ssr_and_api_row_fields_differ")
        return {
            "ssr_ids": entry["job_ids"],
            "api_ids": listing["job_ids"],
            "ssr_count": entry["count"],
            "api_count": listing["count"],
            "same_ids_and_order": same_ids,
            "changed_fields_by_job_id": changed,
            "issues": issues,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": CAREERS_URL,
            "listing_authority": "JUSPAY's own official /careers page and the exact job records its JobListings component publishes.",
            "listing_membership_request": {
                "method": "GET",
                "url": LISTING_API_URL,
                "query_shape": {"limit": "1000", "isGlobal": "true"},
                "filters_applied": False,
                "scope": "Juspay's own careers API; the component filters is_global===true and opening_status!==false.",
                "bundle_evidence": "The official page's /_astro/JobListings.*.js bundle issues this exact request and keeps only is_global===true rows.",
            },
            "detail_method": "Each published job's own https://juspay.io/careers/<job_id> page; the full body is div.prose and the apply link is job-scoped joinus.juspay.in?jobId=<job_id>.",
            "request_scope": "Only the official careers page, its exact listing API and the exact job IDs that page exposes; no ATS-wide discovery.",
        })
        try:
            entry = await self._entry()
            listing = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Juspay source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        membership = self._membership(entry, listing)
        self.result.errors.extend(membership["issues"])
        self.result.expected_total = listing["count"]
        self.result.pages = 1
        self.result.metadata["initial_official_entry"] = {key: value for key, value in entry.items() if key != "rows"}
        self.result.metadata["initial_listing_api"] = {key: value for key, value in listing.items() if key != "rows"}
        self.result.metadata["initial_membership"] = membership
        self.result.timings["initial_source_seconds"] = max(0.0, time.perf_counter() - source_started)

        rows_by_id = {row["job_id"]: row for row in listing["rows"]}
        jobs, detail_seconds, parse_started = [], 0.0, time.perf_counter()
        for identity in listing["job_ids"]:
            row = rows_by_id[identity]
            detail_url = DETAIL_URL.format(job_id=identity)
            try:
                detail = await self._detail(row, detail_url)
            except Exception as exc:
                self.result.errors.append(f"Juspay detail failed {identity} {detail_url}: {type(exc).__name__}: {exc}")
                continue
            detail_seconds += detail["seconds"]
            jobs.append(self._make_job(row, detail))
        self.result.timings["detail_request_seconds"] = detail_seconds
        self.result.timings["detail_phase_seconds"] = max(0.0, time.perf_counter() - parse_started)
        if len(jobs) != listing["count"]:
            self.result.errors.append("juspay_detail_rows_do_not_match_listing_count")

        final_started = time.perf_counter()
        final = None
        try:
            final_entry = await self._entry()
            final_listing = await self._listing()
            final = self._membership(final_entry, final_listing)
            final_ids = final_listing["job_ids"]
            changed = final["changed_fields_by_job_id"]
            initial_rows = {row["job_id"]: row for row in listing["rows"]}
            final_rows = {row["job_id"]: row for row in final_listing["rows"]}
            for identity in listing["job_ids"]:
                if identity in initial_rows and identity in final_rows and initial_rows[identity] != final_rows[identity]:
                    changed[identity] = sorted(
                        key for key in set(initial_rows[identity]) | set(final_rows[identity])
                        if initial_rows[identity].get(key) != final_rows[identity].get(key)
                    )
            complete = (
                entry["job_ids"] == final_entry["job_ids"] == final_ids == listing["job_ids"]
                and not final["issues"] and not changed
                and not final_entry["issues"] and not final_listing["issues"]
            )
            self.result.metadata["final_official_entry"] = {key: value for key, value in final_entry.items() if key != "rows"}
            self.result.metadata["final_listing_api"] = {key: value for key, value in final_listing.items() if key != "rows"}
            final["changed_fields_by_job_id"] = changed
            final["complete"] = complete
            self.result.metadata["final_membership"] = final
            if not complete:
                self.result.errors.append("juspay_listing_membership_changed_during_collection")
            self.result.errors.extend(final["issues"])
        except Exception as exc:
            self.result.errors.append(f"Juspay final source recheck failed: {type(exc).__name__}: {exc}")
            self.result.metadata["final_membership"] = {"complete": False}
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        self.result.jobs = jobs
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["api_count"] = listing["count"]
        self.result.metadata["detail_pages_requested"] = len(jobs)
        self.result.metadata["description_comparison"] = {
            job.job_id: {
                "page_chars": job.raw["detail"]["description_normalized_chars"],
                "api_chars": job.raw["detail"]["api_description_normalized_chars"],
                "page_fnv1a64": job.raw["detail"]["description_fnv1a64"],
                "api_fnv1a64": job.raw["detail"]["api_description_fnv1a64"],
                "matches": job.raw["detail"]["matches_api_description_text"],
            } for job in jobs
        }
        self.result.listing_complete = bool(
            final and final.get("complete")
            and len(jobs) == listing["count"]
            and not self.result.errors
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
