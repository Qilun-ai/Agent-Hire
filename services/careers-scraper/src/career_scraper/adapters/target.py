"""Target's own corporate job search and its exact native job pages.

Membership authority is Target's unfiltered job search at
https://corporate.target.com/api/jobsearch as used by the company-owned
Job Search page.  The rendered page defaults to an IP-derived store radius,
so the UI is loaded with ignoreAutoLocation=true (or the location filter is
cleared) and the same request shape is used here: no lat/lon, no radius and no
country filter.  Target's own /jobs/<w>/<n>/<slug> pages are the only detail
source; the Workday apply links are retained as published and never submitted.

The search API can duplicate a row across adjacent pages while the index is
reordered.  A pass therefore unions its pages, and passes repeat until the
union reaches the advertised count.  A row is never dropped merely because a
single pass repeated it.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Result, now, text_html


OFFICIAL_ENTRY = "https://corporate.target.com/careers/job-search"
ORIGIN = "https://corporate.target.com"
LISTING_API = ORIGIN + "/api/jobsearch"
APPLY_HOST = "target.wd5.myworkdayjobs.com"
PAGE_SIZE = 200
MAX_LISTING_PASSES = 6
DETAIL_METHOD = "target.corporate.job-detail.jsonld"
JOB_PATH = re.compile(r"^/jobs/w\d+/\d+/.+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?$")
TITLE_WS = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")
SALARY_RANGE = re.compile(
    r"(?:pay range(?: per hour)?(?: is)?|the pay range is|starting at|starts at|"
    r"starting hourly rate(?: / salario por hora inicial)?\s*:)\s*"
    r"\$([\d,]+(?:\.\d+)?)(?:\s*(?:-|to|–)\s*\$([\d,]+(?:\.\d+)?))?",
    re.I,
)
SALARY_BARE_RANGE = re.compile(
    r"\$([\d,]+(?:\.\d+)?)\s*(?:-|to|–)\s*\$([\d,]+(?:\.\d+)?)\s*(?:USD)?\s*"
    r"(per hour|hourly|annually|a year|per year)",
    re.I,
)
QUAL_HEADINGS = (
    "what we are looking for",
    "minimum qualifications",
    "preferred qualifications",
    "qualifications",
    "about you",
    "requirements",
    "what you'll need",
    "what you will need",
)
RESP_HEADINGS = (
    "roles & responsibilities",
    "roles and responsibilities",
    "responsibilities",
    "what you'll do",
    "what you will do",
    "a typical day",
)
SECTION_STOPS = (
    "benefits",
    "benefits eligibility",
    "americans with disabilities",
    "equal opportunity",
    "about us",
    "what we are looking for",
    "minimum qualifications",
    "preferred qualifications",
    "qualifications",
    "about you",
    "requirements",
    "roles & responsibilities",
    "roles and responsibilities",
    "responsibilities",
    "what you'll do",
    "what you will do",
    "a typical day",
    "job duties may change",
)
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalized_title(value: str | None) -> str:
    return TITLE_WS.sub(" ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _absolute(path: str | None) -> str | None:
    if not path:
        return None
    return urljoin(ORIGIN + "/", path)


def _date_only(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
    return match.group(1) if match else None


def _title_matches(listing: str, detail: str) -> bool:
    left, right = normalized_title(listing).casefold(), normalized_title(detail).casefold()
    if not left or not right:
        return False
    return left == right or right.startswith(left) or left.startswith(right)


def _same_job_url(requested: str, final: str) -> bool:
    left, right = urlsplit(requested), urlsplit(final)
    if left.netloc != right.netloc:
        return False
    def clean(path):
        return unquote(path).replace("\u200b", "").replace("\u200c", "").rstrip("/")
    return clean(left.path) == clean(right.path)


def _clean_fr_view(node) -> str:
    clone = BeautifulSoup(str(node), "lxml").select_one("div.fr-view")
    if clone is None:
        return ""
    for tag in clone.select("script,style,noscript"):
        tag.decompose()
    for tag in clone.select(
        "div[data-requisition-id],div[data-job-profile],div[data-sub-job-family],"
        "div[data-job-family],div[data-category],div[id^=chq_]"
    ):
        tag.decompose()
    text = clone.get_text("\n", strip=True)
    text = re.sub(r"^\s*Job Id:\s*\S+\s*", "", text, count=1, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


BAD_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _json_ld(soup: BeautifulSoup) -> tuple[dict | None, bool]:
    """Return the JobPosting object and whether a malformed escape was repaired.

    One Target posting (R0000454454) publishes a literal backslash before an
    escaped HTML tag (`concisely\\&lt;/li&gt;`), which is not valid JSON.  The
    visible page contains that same backslash.  Doubling only the invalid
    backslash preserves the published character and makes the object readable.
    """
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.get_text()
        repaired = False
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            try:
                value = json.loads(BAD_JSON_ESCAPE.sub(r"\\\\", text))
                repaired = True
            except (TypeError, ValueError):
                continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            return value, repaired
    return None, False


def _split_address(address: dict | None) -> dict | None:
    if not isinstance(address, dict):
        return None
    def clean(key):
        raw = address.get(key)
        return unescape(raw).strip() if isinstance(raw, str) else raw

    value = {
        "location": ", ".join(
            part for part in (
                clean("streetAddress"), clean("addressLocality"),
                clean("addressRegion"), clean("addressCountry"), clean("postalCode"),
            ) if part
        ) or None,
        "street_address": clean("streetAddress"),
        "city": clean("addressLocality"),
        "state": clean("addressRegion"),
        "country": clean("addressCountry"),
        "postal_code": clean("postalCode"),
    }
    return {key: val for key, val in value.items() if val is not None}


def _salary_from_json_ld(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    inner = value.get("value")
    if not isinstance(inner, dict):
        return None
    minimum, maximum = inner.get("minValue"), inner.get("maxValue")
    if not isinstance(minimum, (int, float)) and not isinstance(maximum, (int, float)):
        return None
    return {
        "min": minimum,
        "max": maximum,
        "currency": value.get("currency"),
        "unit": inner.get("unitText"),
        "source_field": "JobPosting.baseSalary",
    }


def _salary_from_listing(listing: dict) -> dict | None:
    minimum, maximum = listing.get("basepaymin"), listing.get("basepaymax")
    if not isinstance(minimum, (int, float)) and not isinstance(maximum, (int, float)):
        return None
    frequency = (listing.get("basepayfrequency") or "").strip().lower()
    unit = "hour" if frequency == "hourly" else "year" if frequency == "salary" else None
    return {
        "min": minimum,
        "max": maximum,
        "currency": "USD",
        "unit": unit,
        "source_field": "jobsearch.basepay",
    }


def _salary_from_text(value: str) -> dict | None:
    """Parse only explicit dollar ranges/rates; the full text stays in the body."""
    text = value or ""
    candidates = []
    for pattern in (SALARY_RANGE, SALARY_BARE_RANGE):
        for match in pattern.finditer(text):
            def number(raw):
                return float(raw.replace(",", "")) if raw else None
            minimum, maximum = number(match.group(1)), number(match.group(2))
            if minimum is None:
                continue
            context = (match.group(0) + " " + text[match.end():match.end() + 80]).lower()
            unit = "hour" if ("per hour" in context or "hourly" in context) else (
                "year" if any(word in context for word in ("annual", "a year", "per year", "salary")) else None
            )
            if unit == "hour" and minimum < 7.25:
                continue
            if unit == "year" and minimum < 1000:
                continue
            candidates.append(({
                "min": minimum,
                "max": maximum,
                "currency": "USD",
                "unit": unit,
                "source_text": match.group(0).strip(),
                "source_field": "description",
            }, maximum is not None, unit is not None))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[1], item[2], item[0]["min"]))[0]


def _sections(text: str) -> tuple[str | None, str | None]:
    """Verbatim labelled sections only; never synthesised text."""
    lowered = text.casefold()

    def section(headings):
        for heading in headings:
            start = lowered.find(heading)
            if start < 0:
                continue
            start += len(heading)
            end = len(text)
            for stop in SECTION_STOPS:
                found = lowered.find(stop, start)
                if found >= 0:
                    end = min(end, found)
            block = text[start:end].strip(" :-\n\t")
            if len(block) >= 40 and block in text:
                return block
        return None

    return section(QUAL_HEADINGS), section(RESP_HEADINGS)


def parse_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    if response_url != OFFICIAL_ENTRY:
        raise ValueError("Target official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title != "Job Search":
        raise ValueError(f"Target official job-search page title changed: {title!r}")
    block = re.search(r"window\.Search\s*=\s*\{(.*?)\n\s*\}", html, re.S)
    if block is None:
        raise ValueError("Target official page no longer publishes window.Search")
    page_size = re.search(r"\bpageSize\s*:\s*(\d+)", block.group(1))
    areas = re.search(r"\bjobAreaData\s*:\s*(\[[^\]]*\])", block.group(1))
    hierarchy = re.search(r"\bjobHierarchyData\s*:\s*(\[[^\]]*\])", block.group(1))
    try:
        area_data = json.loads(areas.group(1)) if areas else None
        hierarchy_data = json.loads(hierarchy.group(1)) if hierarchy else None
    except ValueError as exc:
        raise ValueError("Target official job-search widget arrays are not readable") from exc
    if (page_size is None or int(page_size.group(1)) != 15
            or not isinstance(area_data, list)
            or hierarchy_data != ["Corporate", "Stores", "Supply Chain"]):
        raise ValueError("Target official job-search widget configuration changed")
    widget = re.search(r"widgetSelector:\s*'([^']+)'", html)
    if not widget or "SearchJobsWidget" not in html:
        raise ValueError("Target official page no longer publishes its SearchJobsWidget")
    return {
        "url": response_url,
        "title": title,
        "page_size": 15,
        "job_area_data": area_data,
        "job_hierarchy_data": hierarchy_data,
        "widget_selector": widget.group(1),
        "search_widget": "SearchJobsWidget",
        "listing_api": LISTING_API,
    }


def parse_listing_page(value: dict, requested_page: int, page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Target job search response is not an object")
    count, results = value.get("count"), value.get("results")
    if not isinstance(count, int) or count < 0 or not isinstance(results, list):
        raise ValueError("Target job search response is missing count/results")
    pages = (count + page_size - 1) // page_size
    if requested_page < 1 or requested_page > pages + 1:
        raise ValueError("Target job search page is outside the advertised pagination")
    expected = 0 if requested_page > pages else (
        page_size if requested_page < pages else count - (pages - 1) * page_size
    )
    issues, rows, seen, duplicate_ids = [], [], set(), []
    for result in results:
        document = result.get("document") if isinstance(result, dict) else None
        if not isinstance(document, dict):
            issues.append("target_listing_row_not_object")
            continue
        identity = document.get("sys_id")
        requisition = document.get("requisitionid")
        title = normalized_title(document.get("title"))
        url = document.get("url")
        if not isinstance(identity, str) or not identity or not isinstance(requisition, str) or not requisition:
            issues.append("target_listing_row_missing_identity")
            continue
        if identity in seen:
            duplicate_ids.append(identity)
            issues.append(f"target_listing_row_duplicate_on_page:{identity}")
            continue
        if (not title or not isinstance(url, str) or not JOB_PATH.fullmatch(url)
                or url.rstrip("/").rsplit("/", 1)[-1] in {"", ".", ".."}):
            issues.append(f"target_listing_row_missing_detail_route:{identity}")
            continue
        apply_url = document.get("applyurl")
        if not isinstance(apply_url, str) or urlsplit(apply_url).hostname != APPLY_HOST:
            issues.append(f"target_listing_row_apply_url_not_target_workday:{identity}")
        posted = document.get("dateposted")
        if posted is not None and (not isinstance(posted, str) or not DATE.fullmatch(posted)):
            issues.append(f"target_listing_row_unknown_date:{identity}")
        if not document.get("country") or not document.get("organization"):
            issues.append(f"target_listing_row_missing_scope_fields:{identity}")
        seen.add(identity)
        rows.append(document)
    if len(results) != expected:
        issues.append(f"target_listing_page_row_count_{len(results)}_expected_{expected}")
    if count and not rows and expected:
        issues.append("target_listing_page_empty")
    return {
        "rows": rows,
        "count": count,
        "page": requested_page,
        "page_size": page_size,
        "pages": pages,
        "expected_rows": expected,
        "facets": value.get("facets") if isinstance(value.get("facets"), dict) else {},
        "search_service": value.get("searchservicename"),
        "duplicate_ids": duplicate_ids,
        "issues": issues,
    }


def parse_detail(html: str, response_url: str, listing: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    value, repaired = _json_ld(soup)
    if value is None:
        raise ValueError("Target detail page has no JobPosting JSON-LD")
    identity = value.get("identifier")
    requisition = listing.get("requisitionid")
    if not isinstance(identity, str) or identity != requisition:
        raise ValueError(f"Target detail identifier {identity!r} does not match listing {requisition!r}")
    detail_url = value.get("url")
    if isinstance(detail_url, str):
        detail_url = unescape(detail_url)
    else:
        detail_url = response_url
    if not _same_job_url(response_url, detail_url):
        raise ValueError("Target detail JSON-LD URL does not match the requested company job URL")
    title = normalized_title(unescape(value.get("title") or ""))
    if not title:
        raise ValueError("Target detail title is missing")
    if not _title_matches(listing.get("title") or "", title):
        raise ValueError(
            f"Target title conflict listing={listing.get('title')!r} detail={title!r}"
        )
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Target detail description is missing")
    description_html = unescape(description)
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError("Target detail description is empty or truncated")

    visible = soup.select_one("div.fr-view")
    visible_text = _clean_fr_view(visible) if visible is not None else ""
    desc_norm = re.sub(r"\s+", " ", description_text).strip()
    visible_norm = re.sub(r"\s+", " ", visible_text).strip()
    body_match = bool(
        desc_norm and visible_norm and (
            desc_norm == visible_norm or desc_norm in visible_norm or visible_norm in desc_norm
        )
    )
    issues = []
    if repaired:
        issues.append("target_detail_jsonld_invalid_escape_repaired")
    if visible is None:
        issues.append("target_detail_visible_description_missing")
    elif not body_match:
        issues.append("target_detail_visible_body_differs_from_jsonld")

    posted = _date_only(value.get("datePosted"))
    listing_posted = _date_only(listing.get("dateposted"))
    if posted and listing_posted and posted != listing_posted:
        issues.append(f"target_detail_date_differs_{listing_posted}_{posted}")
    detail_employment = unescape(str(value.get("employmentType") or "")).strip()
    detail_category = unescape(str(value.get("occupationalCategory") or "")).strip()
    if detail_employment and listing.get("scheduletype"):
        if detail_employment.casefold() != str(listing["scheduletype"]).strip().casefold():
            issues.append("target_detail_employment_type_differs")
    if detail_category and listing.get("primarycategory"):
        if detail_category.casefold() != str(listing["primarycategory"]).strip().casefold():
            issues.append("target_detail_category_differs")

    apply_urls = []
    for anchor in soup.select("a.hero-job-detail--apply-button,a.job-apply-button,a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        href = unescape(href)
        if urlsplit(href).hostname == APPLY_HOST and href not in apply_urls:
            apply_urls.append(href)
    listing_apply = listing.get("applyurl")
    if isinstance(listing_apply, str) and listing_apply not in apply_urls:
        apply_urls.append(listing_apply)
    apply_url = apply_urls[0] if apply_urls else None
    if not apply_url:
        issues.append("target_detail_apply_url_missing")

    location = _split_address(
        ((value.get("jobLocation") or {}).get("address") if isinstance(value.get("jobLocation"), dict) else None)
    )
    geo = value.get("jobLocation", {}).get("geo") if isinstance(value.get("jobLocation"), dict) else None
    if location and isinstance(geo, dict):
        location["latitude"] = geo.get("latitude")
        location["longitude"] = geo.get("longitude")
    locations = [location] if location else []
    # Target publishes extra addresses as a short text list in the body.
    extra = []
    if description_text.startswith("This job is also available for the following locations"):
        for line in description_text.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            if line.startswith(("The pay range", "Pay is based", "ALL ABOUT", "Starting Hourly")):
                break
            if re.search(r",\s*[A-Z]{2}\b", line) and len(line) < 200:
                extra.append({"location": line, "source": "detail_description"})
    if extra:
        locations.extend(extra)

    salary = _salary_from_json_ld(value.get("baseSalary"))
    listing_salary = _salary_from_listing(listing)
    if salary and listing_salary:
        if (salary.get("min") != listing_salary.get("min")
                or salary.get("max") != listing_salary.get("max")):
            issues.append("target_detail_salary_differs_from_listing")
    if salary is None:
        salary = listing_salary
    if salary is None:
        salary = _salary_from_text(description_text)
    if salary is not None and not salary.get("source_text"):
        hero = soup.select_one(".hero-job-detail--salary, .hero-job-detail--pay")
        if hero is not None:
            text = re.sub(r"\s+", " ", hero.get_text(" ", strip=True)).strip()
            if text:
                salary["source_text"] = text

    qualifications, responsibilities = _sections(description_text)
    attachments = [
        {"url": unescape(anchor.get("href")), "label": anchor.get_text(" ", strip=True) or None}
        for anchor in soup.select("div.fr-view a[href]")
        if ATTACHMENT.search(anchor.get("href", ""))
    ]
    detail_meta = {
        key: value.get(key) for key in (
            "identifier", "datePosted", "employmentType", "occupationalCategory",
            "title", "url", "hiringOrganization", "jobLocation", "baseSalary",
        ) if key in value
    }
    detail_meta["description_sha256"] = sha256(description_text.encode()).hexdigest()
    detail_meta["description_chars"] = len(description_text)
    return {
        "title": title,
        "identifier": identity,
        "date_posted": posted,
        "employment_type": detail_employment or None,
        "occupational_category": detail_category or None,
        "description_html": description_html,
        "description_text": description_text,
        "apply_url": apply_url,
        "apply_urls": apply_urls,
        "locations": locations,
        "salary": salary,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "attachments": attachments,
        "visible_reference": {
            "normalized_chars": len(visible_norm),
            "normalized_sha256": sha256(visible_norm.encode()).hexdigest(),
            "fnv1a64": fnv1a64(visible_norm),
            "matches_jsonld_description": body_match,
        },
        "detail": detail_meta,
        "issues": issues,
    }


def detail_page_reference(html: str, response_url: str, listing: dict) -> dict:
    parsed = parse_detail(html, response_url, listing)
    text = parsed["description_text"]
    return {
        "title": parsed["title"],
        "description_normalized_chars": len(re.sub(r"\s+", " ", text).strip()),
        "description_fnv1a64": fnv1a64(re.sub(r"\s+", " ", text).strip()),
        "visible_reference": parsed["visible_reference"],
        "matches_listing": parsed["identifier"] == listing.get("requisitionid"),
    }


class TargetRateLimited(RuntimeError):
    pass


class Target(Adapter):
    """Follow Target's own unfiltered job search and exact native job pages."""

    VERIFICATION_REVISION = "target.corporate-jobsearch-jsonld.1"

    @staticmethod
    def _listing_body(page: int) -> str:
        # No lat/lon/maxdistance: the rendered "ignoreAutoLocation" search uses
        # the same unfiltered global membership.  No country/seniority filters.
        return (
            f"currentPage={page}&q=&hierarchy=&remotetype=&jobcategories=&workersubtype="
            f"&scheduletype=&basepayfrequency=&organization=&locationname=&jobaddress="
            f"&profiles=&city=&state=&country=&internshipType=&jobfamily=&subFamilies="
            f"&culture=en-us&filtercondition=&compgrade=&pageSize={PAGE_SIZE}"
        )

    async def listing_page(self, page: int) -> dict:
        if not self.page_guard():
            raise ValueError("Target listing page budget exhausted")
        response = await self.http.json(
            LISTING_API,
            method="POST",
            content=self._listing_body(page),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": ORIGIN,
                "Referer": OFFICIAL_ENTRY,
            },
        )
        return parse_listing_page(response, page)

    async def run(self) -> Result:
        entry_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        self.entry = parse_entry(entry.text, str(entry.url))
        self.result.metadata["official_entry"] = {
            **self.entry,
            "status": entry.status_code,
            "final_url": str(entry.url),
            "fetched_at": now(),
        }
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)

        listing_started = time.perf_counter()
        union: dict[str, dict] = {}
        passes = []
        count = None
        count_changes = []
        total_page_requests = 0
        listing_issues = []
        first_ids, last_ids = [], []
        for pass_number in range(1, MAX_LISTING_PASSES + 1):
            page = 1
            first = await self.listing_page(page)
            total_page_requests += 1
            if count is None:
                count = first["count"]
            elif first["count"] != count:
                count_changes.append({"pass": pass_number, "from": count, "to": first["count"]})
                count = first["count"]
            pass_rows: dict[str, dict] = {}
            page_counts = []
            duplicates = 0
            new_ids = 0
            duplicates += len(first["duplicate_ids"])
            for row in first["rows"]:
                identity = row["sys_id"]
                if identity in pass_rows:
                    duplicates += 1
                else:
                    pass_rows[identity] = row
                if identity not in union:
                    union[identity] = row
                    new_ids += 1
            page_counts.append({"page": 1, "rows": len(first["rows"]), "expected": first["expected_rows"],
                                "page_duplicates": len(first["duplicate_ids"])})
            if first["issues"]:
                listing_issues.append({"pass": pass_number, "page": 1, "issues": first["issues"]})
            pages = first["pages"]
            for page in range(2, pages + 1):
                value = await self.listing_page(page)
                total_page_requests += 1
                if value["count"] != count:
                    count_changes.append({"pass": pass_number, "page": page, "from": count, "to": value["count"]})
                    count = value["count"]
                    pages = (count + PAGE_SIZE - 1) // PAGE_SIZE
                page_duplicates = len(value["duplicate_ids"])
                for row in value["rows"]:
                    identity = row["sys_id"]
                    if identity in pass_rows:
                        duplicates += 1
                    else:
                        pass_rows[identity] = row
                    if identity not in union:
                        union[identity] = row
                        new_ids += 1
                page_counts.append({
                    "page": page, "rows": len(value["rows"]),
                    "expected": value["expected_rows"], "page_duplicates": page_duplicates,
                })
                if value["issues"]:
                    listing_issues.append({"pass": pass_number, "page": page, "issues": value["issues"]})
            pass_record = {
                "pass": pass_number,
                "count": count,
                "rows": sum(item["rows"] for item in page_counts),
                "unique_rows": len(pass_rows),
                "duplicates": duplicates,
                "new_ids": new_ids,
                "union": len(union),
                "page_requests": len(page_counts),
                "page_counts": page_counts,
            }
            passes.append(pass_record)
            if len(union) >= count:
                break
        first_ids = [next(iter(union))] if union else []
        last_ids = [list(union)[-1]] if union else []
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.timings["listing_phase_seconds"] = listing_seconds
        self.result.pages = total_page_requests
        self.result.expected_total = count
        self.result.metadata["listing"] = {
            "endpoint": LISTING_API,
            "method": "POST",
            "request_body": self._listing_body(1),
            "page_size": PAGE_SIZE,
            "advertised_count": count,
            "passes": passes,
            "pass_count": len(passes),
            "page_requests": total_page_requests,
            "union_count": len(union),
            "duplicates_observed": sum(item["duplicates"] for item in passes),
            "count_changes": count_changes,
            "issues": listing_issues[:200],
            "issue_count": sum(len(item["issues"]) for item in listing_issues),
            "first_ids": first_ids,
            "last_ids": last_ids,
            "ids_sha256": sha256("\n".join(sorted(union)).encode()).hexdigest(),
        }
        if count is not None and len(union) < count:
            self.result.errors.append(
                f"target_listing_union_incomplete_{len(union)}_of_{count}"
            )
        if count is not None and len(union) > count:
            self.result.warnings.append(
                f"target_listing_union_exceeds_advertised_count_{len(union)}_{count}"
            )
        self.result.listing_complete = bool(count is not None and len(union) == count)
        if count_changes:
            self.result.errors.append("target_listing_count_changed_during_collection")

        self.result.metadata["membership_reconciliation"] = {
            "advertised_count": count,
            "unique_ids_collected": len(union),
            "duplicates_observed": sum(item["duplicates"] for item in passes),
            "passes": len(passes),
            "listing_complete": self.result.listing_complete,
        }
        if not union:
            self.result.errors.append("target_listing_empty")
            return self.result

        for document in union.values():
            self.add(self._listing_job(document))

        detail_started = time.perf_counter()
        await self._hydrate_details(self.result.jobs)
        self.result.timings["detail_phase_seconds"] = max(0.0, time.perf_counter() - detail_started)

        recheck_started = time.perf_counter()
        try:
            recheck = await self.listing_page(1)
            self.result.metadata["final_listing_recheck"] = {
                "count": recheck["count"],
                "page_rows": len(recheck["rows"]),
                "advertised_count_before": count,
                "changed": recheck["count"] != count,
                "issues": recheck["issues"],
            }
            if recheck["count"] != count:
                self.result.errors.append("target_listing_count_changed_during_details")
                self.result.listing_complete = False
        except Exception as exc:
            self.result.errors.append(f"target_final_listing_recheck_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        self.result.timings["final_listing_recheck_seconds"] = max(0.0, time.perf_counter() - recheck_started)

        self.result.metadata["field_coverage"] = {
            "date_posted": sum(bool(job.date_posted) for job in self.result.jobs),
            "salary": sum(job.salary is not None for job in self.result.jobs),
            "apply_url": sum(bool(job.apply_url) for job in self.result.jobs),
            "locations": sum(bool(job.locations) for job in self.result.jobs),
            "description_verified": sum(job.description_verified for job in self.result.jobs),
        }
        return self.result

    def _listing_job(self, document: dict):
        job_url = _absolute(document.get("url"))
        location = {
            "location": document.get("jobaddress"),
            "city": document.get("city"),
            "state": document.get("state") or document.get("stateabbreviated"),
            "country": document.get("country"),
            "postal_code": document.get("zipcode"),
            "latitude": document.get("latitude"),
            "longitude": document.get("longitude"),
            "location_name": document.get("locationname"),
            "is_primary": document.get("isprimarylocation"),
            "has_multiple_locations": document.get("hasmultiplelocations"),
            "location_count": document.get("locationcount"),
        }
        locations = [{key: val for key, val in location.items() if val is not None}]
        return self.job(
            raw={"listing": document, "listing_url": job_url},
            title=normalized_title(document.get("title")),
            url=job_url,
            job_id=document.get("requisitionid"),
            requisition_id=document.get("requisitionid"),
            apply_url=document.get("applyurl"),
            locations=locations,
            department=document.get("primarycategory"),
            team=document.get("jobfamily"),
            employment_type=document.get("scheduletype"),
            workplace_type=document.get("remotetype"),
            date_posted=document.get("dateposted"),
            date_posted_raw=document.get("dateposted"),
            employer=document.get("organization"),
            language=document.get("sys_culture"),
            salary=_salary_from_listing(document),
        )

    async def _hydrate_details(self, jobs):
        started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        stats = {
            "total": len(jobs), "checked": 0, "fetched_jobs": 0,
            "verified": 0, "failed": 0, "rate_limited": False,
            "skipped_after_rate_limit": 0, "retried": 0, "retry_recovered": 0,
            "verified_cache_hits": 0,
        }
        self.result.metadata["details"] = stats
        failed = []
        rate_limited = False
        completed = 0

        async def one(job):
            nonlocal completed, rate_limited
            cached = (self.options.cached_jobs or {}).get(job.url)
            if (cached is not None and cached.description_verified
                    and (cached.record_identity is None or cached.key == job.key)):
                self._copy_cached(job, cached)
                stats["verified_cache_hits"] += 1
                completed += 1
                stats["checked"] = completed
                return job
            if rate_limited:
                stats["skipped_after_rate_limit"] += 1
                job.description_unavailable = True
                job.warnings.append("not_fetched_after_rate_limit")
                completed += 1
                stats["checked"] = completed
                return job
            async with sem:
                if rate_limited:
                    stats["skipped_after_rate_limit"] += 1
                    job.description_unavailable = True
                    job.warnings.append("not_fetched_after_rate_limit")
                else:
                    stats["fetched_jobs"] += 1
                    try:
                        await self._detail_once(job)
                        stats["verified"] += 1
                    except TargetRateLimited:
                        rate_limited = True
                        stats["rate_limited"] = True
                        job.description_unavailable = True
                        job.warnings.append("rate_limited")
                        failed.append({"id": job.job_id, "url": job.url, "reason": "HTTP 429/403"})
                    except Exception as exc:
                        stats["failed"] += 1
                        job.description_unavailable = True
                        job.warnings.append("detail_fetch_failed")
                        reason = f"{type(exc).__name__}: {exc}"
                        job.raw["detail_error"] = reason
                        failed.append({"id": job.job_id, "url": job.url, "reason": reason})
                        self.result.errors.append(f"Detail {job.url}: {reason}")
                completed += 1
                stats["checked"] = completed
                return job

        await asyncio.gather(*(one(job) for job in jobs))
        stats["duration_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.timings["detail_first_pass_seconds"] = stats["duration_seconds"]
        stats["verified"] = sum(job.description_verified for job in jobs)

        # One sequential retry for transient failures; permanent 404/410s stay failed.
        retryable = [
            item for item in failed
            if not any(token in item["reason"] for token in ("404", "410", "429", "403"))
        ]
        if retryable and not rate_limited:
            retry_started = time.perf_counter()
            by_id = {job.job_id: job for job in jobs}
            for item in retryable:
                job = by_id.get(item["id"])
                if job is None:
                    continue
                stats["retried"] += 1
                try:
                    await self._detail_once(job)
                    stats["retry_recovered"] += 1
                    stats["failed"] -= 1
                    stats["verified"] += 1
                except Exception as exc:
                    job.raw["detail_retry_error"] = f"{type(exc).__name__}: {exc}"
            self.result.timings["detail_retry_phase_seconds"] = max(
                0.0, time.perf_counter() - retry_started
            )
        self.result.metadata["failed_details"] = failed
        stats["failed_jobs"] = failed

    @staticmethod
    def _copy_cached(job, cached):
        job.title = cached.title or job.title
        job.description_html = cached.description_html
        job.description_text = cached.description_text
        job.description_method = cached.description_method
        job.description_source_url = cached.description_source_url
        job.description_unavailable = cached.description_unavailable
        job.date_posted = cached.date_posted
        job.date_posted_raw = cached.date_posted_raw
        job.date_updated = cached.date_updated
        job.valid_through = cached.valid_through
        job.apply_url = cached.apply_url or job.apply_url
        job.locations = cached.locations or job.locations
        job.department = cached.department or job.department
        job.team = cached.team or job.team
        job.employment_type = cached.employment_type or job.employment_type
        job.workplace_type = cached.workplace_type or job.workplace_type
        job.salary = cached.salary or job.salary
        job.qualifications = cached.qualifications
        job.responsibilities = cached.responsibilities
        job.employer = cached.employer or job.employer
        job.language = cached.language or job.language
        for key in ("detail", "detail_visible_reference", "detail_apply_urls", "attachments"):
            if key in cached.raw:
                job.raw[key] = cached.raw[key]
        job.warnings = [w for w in cached.warnings if w not in (
            "short_or_missing_description", "no_full_description_provenance",
            "possibly_truncated_description",
        )]
        job.validate()
        return job

    async def _detail_once(self, job):
        try:
            response = await self.http.get(job.url)
        except Exception as exc:
            text = str(exc)
            if "429" in text or "403" in text:
                raise TargetRateLimited(text) from exc
            raise
        final_url = str(response.url)
        if not _same_job_url(job.url, final_url):
            raise ValueError(f"Target detail redirected away from its exact job URL: {final_url}")
        listing = job.raw["listing"]
        parsed = parse_detail(response.text, final_url, listing)
        description = parsed["description_text"]
        if len(description.strip()) < 100:
            raise ValueError("Target detail description is empty or truncated")
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_text = description
        job.description_method = DETAIL_METHOD
        job.description_source_url = job.url
        job.description_unavailable = False
        job.date_posted = listing.get("dateposted") or (
            parsed["date_posted"] and parsed["date_posted"] + "T00:00:00Z"
        )
        job.date_posted_raw = listing.get("dateposted")
        job.date_updated = None
        job.valid_through = None
        job.apply_url = parsed["apply_url"] or listing.get("applyurl")
        if parsed["locations"]:
            job.locations = parsed["locations"]
        job.department = parsed["occupational_category"] or listing.get("primarycategory")
        job.team = listing.get("jobfamily")
        job.employment_type = parsed["employment_type"] or listing.get("scheduletype")
        job.workplace_type = listing.get("remotetype")
        job.salary = parsed["salary"] or _salary_from_listing(listing)
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.employer = (
            (parsed["detail"].get("hiringOrganization") or {}).get("name")
            if isinstance(parsed["detail"].get("hiringOrganization"), dict) else None
        ) or listing.get("organization")
        job.language = listing.get("sys_culture")
        job.raw["detail"] = parsed["detail"]
        job.raw["detail_visible_reference"] = parsed["visible_reference"]
        job.raw["detail_apply_urls"] = parsed["apply_urls"]
        job.raw["attachments"] = parsed["attachments"]
        if parsed["issues"]:
            job.warnings.extend(parsed["issues"])
        for warning in parsed["issues"]:
            if warning.startswith("target_detail_visible_body_differs"):
                self.result.errors.append(f"{job.job_id}: {warning}")
        if not parsed["visible_reference"]["matches_jsonld_description"]:
            self.result.warnings.append(f"target_visible_body_differs:{job.job_id}")
        job.validate()
        return job
