"""PayU's own global careers page, its server-rendered job board, and the exact jobs it publishes.

Journey inspected 2026-11-08:
  https://corporate.payu.com/careers/  ->  https://corporate.payu.com/job-board/
The board is a WordPress page rendered by PayU itself.  It reports
"Showing 9 of 9 positions", exposes department/location query-string filters
(no AJAX, no pagination) and links every row to a PayU-owned detail page
``/job/<uuid>/?gh_jid=<uuid>``.  Each detail page renders the full posting and
one Apply link to ``jobs.lever.co/payugpo/<same uuid>/apply``.  The public
Lever account is used only to cross-check those exact nine linked IDs and to
read the job page's schema.org JobPosting metadata; it is never used as a
membership source.  Lever's ``createdAt``/``datePosted`` are preserved raw but
are not mapped to canonical posting dates because neither PayU's listing nor
its detail pages publish a posting date (Gojek/Meesho date precedent).
"""
from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from html import escape
import asyncio
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://corporate.payu.com/careers/"
LISTING_URL = "https://corporate.payu.com/job-board/"
LEVER_ACCOUNT = "payugpo"
LEVER_API = f"https://api.lever.co/v0/postings/{LEVER_ACCOUNT}?mode=json"
LEVER_JOB = f"https://jobs.lever.co/{LEVER_ACCOUNT}/{{job_id}}"
LEVER_APPLY = f"https://jobs.lever.co/{LEVER_ACCOUNT}/{{job_id}}/apply"
EMPLOYER = "PayU GPO"
EXPECTED_TOTAL = 9
JOB_PATH = re.compile(r"^/job/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
RESPONSIBILITY_TOKENS = ("responsibilit", "what will you do", "náplň práce", "key activities")
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "what are we looking for", "skills & knowledge",
    "language qualifications", "who are we looking for", "požadavky",
)
BENEFIT_TOKENS = ("what we offer", "co nabízíme", "our environment", "benefits")
WORKPLACE = {"hybrid": "Hybrid", "remote": "Remote", "on-site": "On-Site", "onsite": "On-Site"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def normalized_body_text(html: str | None) -> str:
    value = text_html(html)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def _check_origin(url: str, host: str, path: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != host or parsed.path.rstrip("/") != path:
        raise ValueError(f"PayU response came from an unexpected route: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _check_origin(response_url, "corporate.payu.com", "/careers")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "careers" not in title.lower():
        raise ValueError("PayU official careers page title changed")
    if not any(normalized_title(h.get_text(" ", strip=True)).lower() == "join our team" for h in soup.select("h1")):
        raise ValueError("PayU official careers page no longer exposes its 'Join our team' heading")
    board_links = sorted({
        urljoin(response_url, a.get("href", ""))
        for a in soup.select("a[href]")
        if urlsplit(urljoin(response_url, a.get("href", ""))).path.rstrip("/") == "/job-board"
    })
    if LISTING_URL not in board_links:
        raise ValueError("PayU official careers page no longer links its /job-board/ listing")
    external_job_links = sorted({
        urljoin(response_url, a.get("href", ""))
        for a in soup.select("a[href]")
        if a.get("href", "").startswith("http")
        and urlsplit(a.get("href", "")).netloc not in ("corporate.payu.com", "www.linkedin.com", "status.paymentsos.com", "developers.payu.com")
    })
    return {
        "url": response_url,
        "title": title,
        "h1": "Join our team",
        "job_board_links": board_links,
        "external_non_job_links": external_job_links,
    }


def parse_job_board(html: str, response_url: str = LISTING_URL) -> dict:
    _check_origin(response_url, "corporate.payu.com", "/job-board")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "job board" not in title.lower():
        raise ValueError("PayU job board title changed")
    job_listing = soup.select_one("div.job-listing")
    if job_listing is None:
        raise ValueError("PayU job board no longer exposes div.job-listing")
    status = job_listing.select_one("div.status")
    if status is None:
        raise ValueError("PayU job board no longer exposes its showing-count status")
    numbers = [int(x.get_text(strip=True)) for x in status.select("span") if x.get_text(strip=True).isdigit()]
    if len(numbers) != 2:
        raise ValueError("PayU job board showing-count status changed")
    showing, total = numbers

    rows, issues, seen = [], [], set()
    for li in job_listing.select("li.job-entry"):
        anchor = li.select_one("a.title[href]")
        if anchor is None:
            issues.append("job_entry_without_title_link")
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        parsed = urlsplit(target)
        match = JOB_PATH.fullmatch(parsed.path)
        query = parse_qs(parsed.query)
        if parsed.scheme != "https" or parsed.netloc != "corporate.payu.com" or match is None:
            issues.append(f"unexpected_job_link:{target}")
            continue
        identity = match.group(1).lower()
        if query.get("gh_jid") != [identity]:
            issues.append(f"job_link_gh_jid_mismatch:{identity}")
        if identity in seen:
            issues.append(f"duplicate_job_id:{identity}")
            continue
        heading = anchor.select_one("h3")
        tags = {tag.get("data-type"): normalized_title(tag.get_text(" ", strip=True))
                for tag in anchor.select("span.tag[data-type]")}
        if not heading or not normalized_title(heading.get_text(" ", strip=True)):
            issues.append(f"job_entry_without_title:{identity}")
            continue
        if not tags.get("title") or not tags.get("location"):
            issues.append(f"job_entry_missing_department_or_location:{identity}")
            continue
        seen.add(identity)
        rows.append({
            "id": identity,
            "url": target,
            "title": normalized_title(heading.get_text(" ", strip=True)),
            "department": tags["title"],
            "location": tags["location"],
        })

    if showing != len(rows) or total != len(rows):
        issues.append(f"job_board_count_mismatch:showing={showing},total={total},rows={len(rows)}")
    if not rows:
        issues.append("payu_job_board_has_no_exposed_job_rows")
    pagination = [
        node for node in soup.select("a[rel=next],a[rel=prev],.pagination,.pager,.page-numbers,.load-more")
    ]
    if pagination:
        issues.append("unexpected_pagination_controls")
    filters = {}
    for name, label in (("department[]", "department"), ("location[]", "location")):
        options = []
        for field in soup.select(f'input[name="{name}"]'):
            option_id = field.get("id")
            option_label = soup.select_one(f'label[for="{option_id}"]') if option_id else None
            options.append({
                "value": field.get("value"),
                "label": normalized_title(option_label.get_text(" ", strip=True)) if option_label else None,
            })
        filters[label] = options
    return {
        "url": response_url,
        "title": title,
        "showing": showing,
        "total": total,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "filters": filters,
        "pagination_controls": len(pagination),
        "issues": list(dict.fromkeys(issues)),
    }


def parse_job_detail(html: str, response_url: str, expected_id: str) -> dict:
    _check_origin(response_url, "corporate.payu.com", f"/job/{expected_id}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "page not found" in title.lower():
        raise ValueError(f"PayU official detail page is a 404: {response_url}")
    h1 = soup.select_one("section.hero-secondary h1")
    if h1 is None or not normalized_title(h1.get_text(" ", strip=True)):
        raise ValueError("PayU official detail page no longer exposes its title heading")
    location = soup.select_one("section.hero-secondary .job-location p")
    department = soup.select_one("section.hero-secondary .job-department p")
    main = soup.select_one("main.job-details-text-block #main")
    if main is None:
        raise ValueError("PayU official detail page no longer exposes its description field")
    body = BeautifulSoup(str(main), "lxml").select_one("#main")
    if body is None:
        raise ValueError("PayU official detail page description could not be re-parsed")
    apply_links = sorted({
        a.get("href", "") for a in body.select("a[href]") if "jobs.lever.co" in a.get("href", "")
    })
    expected_apply = LEVER_APPLY.format(job_id=expected_id)
    if apply_links != [expected_apply]:
        raise ValueError(f"PayU detail page {expected_id} Apply link changed: {apply_links!r}")
    for anchor in body.select("a.btn"):
        anchor.decompose()
    description_html = body.decode_contents()
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"PayU detail page {expected_id} has an empty or implausibly short description")
    html_tag = soup.find("html")
    return {
        "url": response_url,
        "title": normalized_title(h1.get_text(" ", strip=True)),
        "location": normalized_title(location.get_text(" ", strip=True)) if location else None,
        "department": normalized_title(department.get_text(" ", strip=True)) if department else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized": normalized_body_text(description_html),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_chars": len(normalized_body_text(description_html)),
        "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
        "apply_url": expected_apply,
        "apply_text": "Apply for this job",
        "language": html_tag.get("lang") if html_tag and html_tag.get("lang") else "en",
        "page_title": title,
    }


def parse_lever_postings(value: dict | list, expected_ids: list[str] | None = None) -> dict:
    if not isinstance(value, list):
        raise ValueError("PayU's linked Lever public postings response is not a list")
    rows, issues, seen = {}, [], set()
    for row in value:
        if not isinstance(row, dict):
            issues.append("lever_posting_not_an_object")
            continue
        identity = str(row.get("id") or "").lower()
        if UUID.fullmatch(identity) is None:
            issues.append("lever_posting_missing_uuid")
            continue
        if identity in seen:
            issues.append(f"lever_duplicate_id:{identity}")
            continue
        categories = row.get("categories")
        if (not normalized_title(row.get("text")) or not isinstance(categories, dict)
                or not normalized_title(categories.get("team"))
                or not normalized_title(categories.get("location"))
                or not isinstance(row.get("lists"), list)
                or not isinstance(row.get("description"), str)
                or not isinstance(row.get("additional"), str)
                or not isinstance(row.get("createdAt"), (int, float))):
            issues.append(f"lever_posting_source_fields_missing:{identity}")
            continue
        if row.get("applyUrl") != LEVER_APPLY.format(job_id=identity):
            issues.append(f"lever_apply_url_changed:{identity}")
        if row.get("hostedUrl") != LEVER_JOB.format(job_id=identity):
            issues.append(f"lever_hosted_url_changed:{identity}")
        seen.add(identity)
        rows[identity] = row
    ids = list(rows)
    if expected_ids is not None:
        missing = [x for x in expected_ids if x not in rows]
        extra = [x for x in ids if x not in expected_ids]
        if missing:
            issues.append("lever_ids_missing_from_company_listing:" + ",".join(missing))
        if extra:
            issues.append("lever_ids_not_linked_by_company_listing:" + ",".join(extra))
    return {"rows": rows, "ids": ids, "count": len(rows), "issues": issues}


def parse_lever_job_page(html: str, response_url: str, expected_id: str) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "jobs.lever.co"
            or parsed.path.rstrip("/") != f"/{LEVER_ACCOUNT}/{expected_id}"):
        raise ValueError(f"PayU Lever detail page came from an unexpected route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    posting = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            posting = value
            break
    if posting is None:
        raise ValueError(f"PayU Lever detail page {expected_id} no longer exposes JobPosting JSON-LD")
    title = normalized_title(posting.get("title"))
    if not title:
        raise ValueError(f"PayU Lever detail page {expected_id} JobPosting has no title")
    date_posted = posting.get("datePosted")
    if date_posted is not None:
        date_posted = str(date_posted).strip()
        if DATE.fullmatch(date_posted) is None:
            raise ValueError(f"PayU Lever detail page {expected_id} datePosted is not YYYY-MM-DD: {date_posted!r}")
    organization = posting.get("hiringOrganization")
    employer = normalized_title(organization.get("name")) if isinstance(organization, dict) else None
    if employer != EMPLOYER:
        raise ValueError(f"PayU Lever detail page {expected_id} employer changed: {employer!r}")
    location = posting.get("jobLocation")
    locality = None
    if isinstance(location, dict):
        address = location.get("address")
        if isinstance(address, dict):
            locality = normalized_title(address.get("addressLocality"))
    return {
        "url": response_url,
        "job_id": expected_id,
        "title": title,
        "date_posted": date_posted,
        "employment_type": normalized_title(posting.get("employmentType")) or None,
        "employer": employer,
        "location": locality,
        "jsonld": posting,
    }


def lever_reference_text(row: dict) -> str:
    parts = [row.get("description") or ""]
    parts.extend(item.get("content") or "" for item in row.get("lists") or [] if isinstance(item, dict))
    parts.append(row.get("additional") or "")
    return normalized_body_text(" ".join(parts))


def description_without_headings(html: str) -> str:
    soup = BeautifulSoup(f'<div data-payu-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-payu-body]")
    if root is None:
        return ""
    for heading in root.find_all(HEADING_TAGS):
        heading.decompose()
    return normalized_body_text(root.decode_contents())


def fallback_detail_from_lever(row: dict, lever_jsonld: dict) -> dict:
    """Explicit same-job fallback used only when PayU's own detail page fails.

    The exact UUID came from PayU's listing and the same UUID is exposed by the
    Lever apply page linked from every working PayU detail page.  The fallback
    is labelled and recorded as a failure; it is never silent.
    """
    identity = row["id"]
    parts = [row.get("description") or ""]
    for item in row.get("lists") or []:
        if isinstance(item, dict) and item.get("text"):
            parts.append(f"<h3>{escape(str(item['text']).strip())}</h3>")
            parts.append(item.get("content") or "")
    parts.append(row.get("additional") or "")
    description_html = "\n".join(parts)
    normalized = normalized_body_text(description_html)
    return {
        "url": LEVER_JOB.format(job_id=identity),
        "title": normalized_title(row.get("text")),
        "location": normalized_title((row.get("categories") or {}).get("location")),
        "department": normalized_title((row.get("categories") or {}).get("team")),
        "description_html": description_html,
        "description_text": text_html(description_html),
        "description_normalized": normalized,
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_chars": len(normalized),
        "description_fnv1a64": fnv1a64(normalized),
        "apply_url": LEVER_APPLY.format(job_id=identity),
        "apply_text": "Apply for this job",
        "language": "en",
        "page_title": f"PayU GPO - {normalized_title(row.get('text'))}",
        "fallback_used": True,
        "lever_date_posted": lever_jsonld.get("date_posted"),
    }


def description_sections(row: dict) -> list[dict]:
    sections, issues = [], []
    for item in row.get("lists") or []:
        if not isinstance(item, dict):
            issues.append("lever_list_not_an_object")
            continue
        heading = normalized_title(item.get("text"))
        content_html = item.get("content") or ""
        content_text = normalized_body_text(content_html)
        if not heading or not content_text:
            issues.append("lever_list_missing_heading_or_content")
            continue
        lower = heading.lower().rstrip(":")
        if any(token in lower for token in RESPONSIBILITY_TOKENS):
            role = "responsibilities"
        elif any(token in lower for token in QUALIFICATION_TOKENS):
            role = "qualifications"
        elif any(token in lower for token in BENEFIT_TOKENS):
            role = "benefits"
        else:
            role = "other"
        sections.append({
            "heading": heading,
            "role": role,
            "html": content_html,
            "text": content_text,
            "normalized_chars": len(content_text),
            "sha256": sha256(content_text.encode()).hexdigest(),
        })
    return sections


def posting_kind(title: str, department: str | None) -> str:
    value = f"{title} {department or ''}".lower()
    if "talent pool" in value:
        return "talent_pool"
    if "evergreen" in value or "pipeline" in value:
        return "evergreen_intake"
    return "job"


class Payu(Adapter):
    VERIFICATION_REVISION = "payu.official-job-board.lever-linked-details.1"

    async def _get_with_retry(self, url: str, *, attempts: int = 3):
        """Retry a transient WordPress 404, but never retry a challenge/429."""
        errors = []
        for attempt in range(attempts):
            try:
                return await self.http.get(url), errors
            except FetchError as exc:
                message = str(exc)
                errors.append(message)
                if any(code in message for code in ("403", "429")):
                    raise
                if "404" not in message or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(1.0 + attempt * 0.5)
        raise FetchError(errors[-1] if errors else f"Request failed: {url}")

    def _make_job(self, row, detail, lever_row, lever_jsonld, sections, comparison) -> object:
        identity = row["id"]
        categories = lever_row.get("categories") or {}
        department = normalized_title(categories.get("team"))
        locations = [normalized_title(categories.get("location"))] if categories.get("location") else []
        workplace_raw = normalized_title(lever_row.get("workplaceType")).lower() or None
        workplace = WORKPLACE.get(workplace_raw, workplace_raw.title() if workplace_raw else None)
        job = self.job(
            {
                "listing": dict(row),
                "official_detail": {
                    key: detail[key] for key in (
                        "url", "title", "location", "department", "description_sha256",
                        "description_chars", "description_fnv1a64", "apply_url", "language", "page_title",
                    )
                },
                "lever_api": dict(lever_row),
                "lever_jsonld": lever_jsonld,
                "sections": sections,
                "description_comparison": comparison,
                "posting_kind": posting_kind(row["title"], department),
                "dates": {
                    "lever_date_posted": lever_jsonld.get("date_posted"),
                    "lever_createdAt": lever_row.get("createdAt"),
                    "lever_createdAt_utc_date": datetime.fromtimestamp(
                        lever_row["createdAt"] / 1000, timezone.utc
                    ).date().isoformat(),
                    "date_posted_policy": (
                        "Neither PayU's listing nor its own detail pages publish a posting date. "
                        "Lever JSON-LD datePosted/createdAt are preserved raw only; canonical dates stay null."
                    ),
                },
                "salary_evidence": None,
                "requisition_evidence": None,
                "employer_source": "Lever JobPosting hiringOrganization for the exact linked ID",
            },
            title=row["title"],
            url=row["url"],
            job_id=identity,
            requisition_id=None,
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="payu.official_job_detail.rendered_full_posting",
            description_url=detail["url"],
            locations=locations,
            department=department,
            team=normalized_title(categories.get("department")) or None,
            employment_type=normalized_title(categories.get("commitment")) or lever_jsonld.get("employment_type"),
            workplace_type=workplace,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=[s for s in sections if s["role"] == "qualifications"] or None,
            responsibilities=[s for s in sections if s["role"] == "responsibilities"] or None,
            employer=EMPLOYER,
            language=detail.get("language") or "en",
        )
        job.description_text = detail["description_text"]
        job.record_identity = f"payu:{identity}"
        if job.raw["posting_kind"] != "job":
            job.warnings.append("talent_pool_or_evergreen_listing")
        return job.validate()

    def _validate_pair(self, row, detail, lever_row, lever_jsonld, sections, comparison) -> list[str]:
        issues = []
        identity = row["id"]
        categories = lever_row.get("categories") or {}
        if detail["title"] != row["title"]:
            issues.append(f"payu_title_mismatch_listing_detail:{identity}")
        if normalized_title(detail["title"]) != normalized_title(lever_row.get("text")):
            issues.append(f"payu_title_mismatch_detail_lever:{identity}")
        if normalized_title(detail["location"]) != normalized_title(categories.get("location")):
            issues.append(f"payu_location_mismatch_detail_lever:{identity}")
        detail_department = normalized_title(detail["department"]).replace("–", "-")
        lever_department = normalized_title(categories.get("team")).replace("–", "-")
        if detail_department != lever_department:
            issues.append(f"payu_department_mismatch_detail_lever:{identity}")
        if detail["location"] != row["location"]:
            issues.append(f"payu_location_mismatch_listing_detail:{identity}")
        listing_department = normalized_title(row["department"]).replace("–", "-")
        if listing_department != lever_department:
            issues.append(f"payu_department_mismatch_listing_lever:{identity}")
        if detail["apply_url"] != lever_row.get("applyUrl"):
            issues.append(f"payu_apply_mismatch_detail_lever:{identity}")
        if lever_jsonld.get("title") != row["title"] or lever_jsonld.get("employer") != EMPLOYER:
            issues.append(f"payu_lever_jsonld_identity_mismatch:{identity}")
        if lever_jsonld.get("location") and normalized_title(lever_jsonld["location"]) != normalized_title(categories.get("location")):
            issues.append(f"payu_lever_jsonld_location_mismatch:{identity}")
        if (lever_jsonld.get("employment_type") and normalized_title(categories.get("commitment"))
                and normalized_title(lever_jsonld["employment_type"]) != normalized_title(categories.get("commitment"))):
            issues.append(f"payu_employment_type_mismatch:{identity}")
        created = datetime.fromtimestamp(lever_row["createdAt"] / 1000, timezone.utc).date().isoformat()
        if lever_jsonld.get("date_posted") and lever_jsonld["date_posted"] != created:
            issues.append(f"payu_lever_datePosted_createdAt_mismatch:{identity}")
        if not comparison["exact_match"]:
            issues.append(f"payu_description_differs_from_linked_lever_posting:{identity}")
        if comparison["ratio"] < 0.99:
            issues.append(f"payu_description_ratio_below_threshold:{identity}:{comparison['ratio']}")
        normalized_body = detail["description_normalized"]
        for section in sections:
            if section["heading"] not in normalized_body:
                issues.append(f"payu_section_heading_not_rendered:{identity}:{section['heading']}")
            if section["text"] not in normalized_body:
                issues.append(f"payu_section_content_not_rendered:{identity}:{section['heading']}")
        return issues

    async def _collect_one(self, row, lever_row, semaphore):
        async with semaphore:
            identity = row["id"]
            detail_started = time.perf_counter()
            transient, detail_error, fallback_used = [], None, False
            detail_response = None
            try:
                detail_response, retry_errors = await self._get_with_retry(row["url"])
                transient.extend(f"official_detail:{identity}:{value}" for value in retry_errors)
                detail = parse_job_detail(detail_response.text, str(detail_response.url), identity)
            except Exception as exc:
                detail_error = f"{type(exc).__name__}: {exc}"
            try:
                lever_response = await self.http.get(LEVER_JOB.format(job_id=identity))
                lever_jsonld = parse_lever_job_page(lever_response.text, str(lever_response.url), identity)
            except Exception as exc:
                duration = max(0.0, time.perf_counter() - detail_started)
                return {
                    "job": None,
                    "issues": [f"payu_linked_lever_detail_failed:{identity}:{type(exc).__name__}: {exc}"],
                    "transient": transient,
                    "duration": duration,
                    "detail_meta": {
                        "url": row["url"],
                        "final_url": None,
                        "status": None,
                        "response_bytes": 0,
                        "response_sha256": None,
                        "request_seconds": duration,
                        "lever_url": f"https://jobs.lever.co/{LEVER_ACCOUNT}/{identity}",
                        "lever_response_sha256": None,
                        "official_detail_error": detail_error,
                        "fallback_used": False,
                    },
                }
            if detail_error:
                fallback_used = True
                detail = fallback_detail_from_lever(lever_row, lever_jsonld)
            sections = description_sections(lever_row)
            reference = lever_reference_text(lever_row)
            actual = description_without_headings(detail["description_html"])
            ratio = SequenceMatcher(None, reference, actual).ratio()
            comparison = {
                "lever_reference_chars": len(reference),
                "official_body_without_headings_chars": len(actual),
                "exact_match": reference == actual,
                "ratio": round(ratio, 6),
                "lever_reference_fnv1a64": fnv1a64(reference),
                "official_body_without_headings_fnv1a64": fnv1a64(actual),
                "method": "Official detail body with its h1-h6 section headings removed is compared with the exact linked Lever posting's description + lists + additional text.",
            }
            issues = self._validate_pair(row, detail, lever_row, lever_jsonld, sections, comparison)
            job = self._make_job(row, detail, lever_row, lever_jsonld, sections, comparison)
            if fallback_used:
                issues.append(f"payu_official_detail_unavailable_used_exact_lever_job:{identity}:{detail_error}")
                job.description_method = "payu.listing.lever_exact_job_fallback"
                job.description_source_url = LEVER_JOB.format(job_id=identity)
                job.raw["official_detail"]["fallback_used"] = True
                job.raw["official_detail"]["official_detail_error"] = detail_error
                job.warnings.append("official_detail_unavailable_used_exact_lever_job")
                job.validate()
            duration = max(0.0, time.perf_counter() - detail_started)
            return {
                "job": job,
                "issues": issues,
                "transient": transient,
                "duration": duration,
                "fallback_used": fallback_used,
                "detail_meta": {
                    "url": row["url"],
                    "final_url": str(detail_response.url) if detail_response is not None else None,
                    "status": detail_response.status_code if detail_response is not None else None,
                    "response_bytes": len(detail_response.content) if detail_response is not None else 0,
                    "response_sha256": sha256(detail_response.content).hexdigest() if detail_response is not None else None,
                    "request_seconds": duration,
                    "lever_url": f"https://jobs.lever.co/{LEVER_ACCOUNT}/{identity}",
                    "lever_response_sha256": sha256(lever_response.content).hexdigest(),
                    "official_detail_error": detail_error,
                    "fallback_used": fallback_used,
                },
            }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "PayU's own server-rendered /job-board/ page, reached from its official /careers/ page.",
            "listing_membership_request": {
                "method": "GET",
                "path": "/job-board/",
                "query_shape": "department[]=...&location[]=... are ordinary GET filters; no AJAX or paging request exists.",
                "pagination": "none; the page reports Showing 9 of 9 positions and exposes no pager/load-more control",
                "filters_applied": False,
                "scope": "The global PayU job board only; no country or seniority filter.",
            },
            "detail_method": "Each official PayU /job/<uuid>/?gh_jid=<uuid> page renders the full posting. The exact same UUID is then checked on the linked public Lever account and its JobPosting JSON-LD.",
            "apply_route": "https://jobs.lever.co/payugpo/<uuid>/apply",
            "employer": EMPLOYER,
            "date_policy": "PayU's listing and detail pages publish no posting date; Lever datePosted/createdAt are preserved only in raw fields and canonical dates stay null.",
            "request_scope": "Only the nine UUIDs exposed by PayU's own /job-board/ page; the Lever account is never used to discover additional jobs.",
        })
        try:
            entry_started = time.perf_counter()
            entry_response = await self.http.get(OFFICIAL_ENTRY)
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            entry.update({
                "status": entry_response.status_code,
                "response_bytes": len(entry_response.content),
                "response_sha256": sha256(entry_response.content).hexdigest(),
                "request_seconds": max(0.0, time.perf_counter() - entry_started),
            })
            listing_started = time.perf_counter()
            listing_response = await self.http.get(LISTING_URL)
            listing = parse_job_board(listing_response.text, str(listing_response.url))
            listing.update({
                "status": listing_response.status_code,
                "response_bytes": len(listing_response.content),
                "response_sha256": sha256(listing_response.content).hexdigest(),
                "request_seconds": max(0.0, time.perf_counter() - listing_started),
            })
            lever_started = time.perf_counter()
            lever_response = await self.http.get(LEVER_API)
            lever_board = parse_lever_postings(lever_response.json(), listing["job_ids"])
            lever_board.update({
                "status": lever_response.status_code,
                "response_bytes": len(lever_response.content),
                "response_sha256": sha256(lever_response.content).hexdigest(),
                "request_seconds": max(0.0, time.perf_counter() - lever_started),
            })
        except Exception as exc:
            self.result.errors.append(f"PayU source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = listing["total"]
        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["initial_listing"] = {k: v for k, v in listing.items() if k != "rows"}
        self.result.metadata["initial_job_ids"] = listing["job_ids"]
        self.result.metadata["lever_board"] = {k: v for k, v in lever_board.items() if k != "rows"}
        self.result.metadata["filter_options"] = listing["filters"]
        self.result.errors.extend(listing["issues"])
        self.result.errors.extend(lever_board["issues"])

        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        detail_started = time.perf_counter()
        collected = await asyncio.gather(*(
            self._collect_one(row, lever_board["rows"][row["id"]], semaphore)
            for row in listing["rows"]
        ))
        self.result.timings["detail_phase_seconds"] = max(0.0, time.perf_counter() - detail_started)
        jobs, transient_errors, failed_detail_ids = [], [], []
        for item in collected:
            if item["job"] is None:
                failed_detail_ids.append(item["detail_meta"]["url"])
                self.result.errors.extend(item["issues"])
                transient_errors.extend(item["transient"])
                continue
            jobs.append(item["job"])
            self.result.errors.extend(item["issues"])
            transient_errors.extend(item["transient"])
        self.result.metadata["transient_detail_errors"] = transient_errors
        self.result.metadata["failed_detail_urls"] = failed_detail_ids
        self.result.metadata["detail_requests"] = [item["detail_meta"] for item in collected]
        self.result.metadata["detail_fetch_count"] = len(collected)
        self.result.metadata["fallback_detail_ids"] = [
            item["job"].job_id for item in collected
            if item["job"] is not None and item.get("fallback_used")
        ]

        final_started = time.perf_counter()
        try:
            final_entry_response = await self.http.get(OFFICIAL_ENTRY)
            final_entry = parse_official_entry(final_entry_response.text, str(final_entry_response.url))
            final_listing_response = await self.http.get(LISTING_URL)
            final_listing = parse_job_board(final_listing_response.text, str(final_listing_response.url))
            final_lever_response = await self.http.get(LEVER_API)
            final_lever = parse_lever_postings(final_lever_response.json(), final_listing["job_ids"])
        except Exception as exc:
            final_entry = final_listing = final_lever = None
            self.result.errors.append(f"PayU final source recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_membership_check_seconds"] = max(0.0, time.perf_counter() - final_started)

        if final_entry is None or final_listing is None or final_lever is None:
            final_matches = False
            reconciliation = {"complete": False}
        else:
            initial_rows = {row["id"]: row for row in listing["rows"]}
            final_rows = {row["id"]: row for row in final_listing["rows"]}
            changed = {
                identity: sorted(key for key in set(initial_rows[identity]) | set(final_rows[identity])
                                  if initial_rows[identity].get(key) != final_rows[identity].get(key))
                for identity in initial_rows.keys() & final_rows.keys()
                if initial_rows[identity] != final_rows[identity]
            }
            same_ids = listing["job_ids"] == final_listing["job_ids"]
            same_counts = (listing["showing"], listing["total"]) == (final_listing["showing"], final_listing["total"])
            same_lever_ids = set(lever_board["ids"]) == set(final_lever["ids"])
            entry_same = final_entry["job_board_links"] == entry["job_board_links"]
            filters_same = final_listing["filters"] == listing["filters"]
            final_matches = bool(
                same_ids and same_counts and same_lever_ids and entry_same and filters_same
                and not changed and not final_listing["issues"] and not final_lever["issues"]
            )
            if not same_ids or not same_counts:
                self.result.errors.append("payu_listing_membership_changed_during_collection")
            if not same_lever_ids:
                self.result.errors.append("payu_lever_membership_changed_during_collection")
            if changed:
                self.result.errors.append("payu_listing_fields_changed_during_collection")
            if not entry_same:
                self.result.errors.append("payu_official_entry_changed_during_collection")
            if not filters_same:
                self.result.errors.append("payu_filter_options_changed_during_collection")
            self.result.errors.extend(final_listing["issues"])
            self.result.errors.extend(final_lever["issues"])
            reconciliation = {
                "initial_ids": listing["job_ids"],
                "final_ids": final_listing["job_ids"],
                "initial_count": listing["total"],
                "final_count": final_listing["total"],
                "initial_showing": listing["showing"],
                "final_showing": final_listing["showing"],
                "initial_lever_ids": lever_board["ids"],
                "final_lever_ids": final_lever["ids"],
                "same_ids_and_order": same_ids,
                "same_counts": same_counts,
                "same_lever_ids": same_lever_ids,
                "official_entry_still_links_job_board": entry_same,
                "filter_options_unchanged": filters_same,
                "changed_fields_by_job_id": changed,
                "new_job_ids": [x for x in final_listing["job_ids"] if x not in listing["job_ids"]],
                "removed_job_ids": [x for x in listing["job_ids"] if x not in final_listing["job_ids"]],
                "complete": final_matches,
            }
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {k: v for k, v in final_listing.items() if k != "rows"}
            self.result.metadata["final_lever_board"] = {k: v for k, v in final_lever.items() if k != "rows"}
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["listing_showing_total"] = listing["total"]
        self.result.metadata["lever_board_count"] = lever_board["count"]
        self.result.metadata["detail_descriptions_verified"] = sum(job.description_verified for job in jobs)
        self.result.jobs = jobs
        self.result.pages = 1
        self.result.listing_complete = bool(
            final_matches and len(jobs) == listing["total"] and len(jobs) == EXPECTED_TOTAL
            and all(job.description_verified for job in jobs)
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
