"""NetApp's own TalentBrew careers site: 22 server-rendered global listing pages.

The official entry https://careers.netapp.com/search-jobs publishes an
unfiltered total (318 at review time), 15 visible cards per page, and numbered
pages.  The same first-party site renders each exact job page.  The full body
is the rendered ``.ats-description``; its final Apply/Save controls are removed
before extraction.  Listing cards and detail JSON-LD are cross-checked rather
than treating a guessed ATS/board as membership.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://careers.netapp.com/search-jobs"
BASE_URL = "https://careers.netapp.com"
BASE_HOST = "careers.netapp.com"
ORG_ID = "27600"
ITEMS_PER_PAGE = 15
LISTING_PATH = "/search-jobs"
JOB_PATH = re.compile(r"^/job/([^/]+)/([^/]+)/27600/(\d+)$", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_visible_text(value: str | Tag | BeautifulSoup) -> str:
    """Whitespace-normalised rendered body, matching Chromium innerText after
    removing the application/save controls that sit inside the body wrapper."""
    if isinstance(value, str):
        root = BeautifulSoup(value, "lxml")
    else:
        root = BeautifulSoup(str(value), "lxml")
    for node in root.select("script,style,noscript,a.job-apply,button.js-save-job-btn,.job-description__buttons"):
        node.decompose()
    return re.sub(r"\s+", " ", root.get_text("")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _meta_map(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for meta in soup.select("meta[name]"):
        name = meta.get("name")
        if name and name not in values:
            values[name] = meta.get("content", "")
    return values


def _int_meta(meta: dict[str, str], name: str) -> int | None:
    value = meta.get(name)
    if value is None or not re.fullmatch(r"\d+", value.strip()):
        return None
    return int(value)


def parse_listing_page(html: str, response_url: str, requested_page: int) -> dict:
    """Parse one exact first-party search page and its native page controls."""
    parsed = urlsplit(response_url)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != BASE_HOST
            or parsed.path.rstrip("/") != LISTING_PATH):
        raise ValueError("NetApp listing response came from an unexpected route")
    if requested_page < 1:
        raise ValueError("NetApp listing page number must be positive")
    expected_query = {} if requested_page == 1 else {"p": [str(requested_page)]}
    if query != expected_query:
        raise ValueError(f"NetApp listing page {requested_page} has unexpected query {query}")

    soup = BeautifulSoup(html, "lxml")
    meta = _meta_map(soup)
    total = _int_meta(meta, "search-analytics-total-jobs")
    skip = _int_meta(meta, "search-analytics-skip")
    if total is None or skip is None or total < 0:
        raise ValueError("NetApp listing total/skip metadata is missing")
    if skip != (requested_page - 1) * ITEMS_PER_PAGE:
        raise ValueError("NetApp listing skip does not match the observed page size")

    current = soup.select_one("input.pagination-current")
    total_pages_node = soup.select_one(".pagination-total-pages")
    if current is None or total_pages_node is None:
        raise ValueError("NetApp listing pagination controls are missing")
    try:
        current_page = int(current.get("value", ""))
        max_page = int(current.get("max", ""))
        total_pages = int(re.search(r"\d+", total_pages_node.get_text(" ", strip=True)).group())
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("NetApp listing pagination values are unreadable") from exc
    if current_page != requested_page or max_page != total_pages:
        raise ValueError("NetApp pagination current/max fields disagree with the request")
    calculated_pages = math.ceil(total / ITEMS_PER_PAGE) if total else 0
    if total_pages != calculated_pages:
        raise ValueError("NetApp pagination total does not match total jobs / observed page size")

    rows, seen, issues = [], set(), []
    for anchor in soup.select("#search-results-list ul li a[data-job-id]"):
        identity = (anchor.get("data-job-id") or "").strip()
        target = urljoin(response_url, anchor.get("href", ""))
        target_parts = urlsplit(target)
        match = JOB_PATH.fullmatch(target_parts.path)
        if (target_parts.scheme != "https" or target_parts.netloc != BASE_HOST
                or not identity.isdigit() or match is None or match.group(3) != identity):
            issues.append(f"netapp_listing_card_identity_invalid:{identity or 'missing'}")
            continue
        title_node = anchor.select_one("h3")
        location_node = anchor.select_one(".job-location")
        if title_node is None or location_node is None:
            issues.append(f"netapp_listing_card_fields_missing:{identity}")
            continue
        if identity in seen:
            issues.append(f"netapp_listing_duplicate_id:{identity}")
            continue
        seen.add(identity)
        rows.append({
            "id": identity,
            "url": target,
            "title": normalized_title(title_node.get_text(" ", strip=True)),
            "location": normalized_title(location_node.get_text(" ", strip=True)),
        })

    expected_rows = min(ITEMS_PER_PAGE, max(0, total - skip))
    if len(rows) != expected_rows or len(rows) > ITEMS_PER_PAGE:
        issues.append("netapp_listing_page_row_count_mismatch")
    if not rows and total:
        issues.append("netapp_listing_page_has_no_cards")

    # The same page carries the ordered list of rendered job IDs.  It is a
    # useful local cross-check that no visible card was skipped or reordered.
    meta_ids = []
    try:
        meta_ids = list(json.loads(meta.get("search-analytics-jobIds", "{}")).keys())
    except (TypeError, ValueError):
        issues.append("netapp_search_analytics_job_ids_unreadable")
    if meta_ids and meta_ids != [row["id"] for row in rows]:
        issues.append("netapp_search_analytics_job_ids_order_mismatch")

    page_count_text = soup.select_one("#pagination-bottom .pagination-page-count")
    return {
        "page": requested_page,
        "total": total,
        "skip": skip,
        "total_pages": total_pages,
        "items_per_page": ITEMS_PER_PAGE,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "pagination_text": normalized_title(page_count_text.get_text(" ", strip=True)) if page_count_text else None,
        "issues": issues,
    }


def _jobposting(soup: BeautifulSoup) -> dict | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates = value if isinstance(value, list) else [value]
        if isinstance(value, dict) and isinstance(value.get("@graph"), list):
            candidates = value["@graph"]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _iso_date(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ][0-9:+\-Z.]+)?$", value.strip())
    return match.group(1) if match else None


def _req_id(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _salary(value):
    if not isinstance(value, dict):
        return None
    currency = value.get("currency")
    amount = value.get("value")
    if isinstance(amount, dict):
        amount = amount.get("value")
    if not currency and amount in (None, "", 0):
        return None
    return value


def _address_text(address: dict) -> str | None:
    parts = [address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry")]
    value = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    return value or None


def _locations(listing_location: str, detail_location: str | None, jobposting: dict | None) -> list[str]:
    values = [part.strip() for part in (listing_location or "").split(";") if part.strip()]
    if not values and detail_location:
        values = [part.strip() for part in detail_location.split(";") if part.strip()]
    if not values and jobposting:
        locations = jobposting.get("jobLocation")
        locations = locations if isinstance(locations, list) else ([locations] if isinstance(locations, dict) else [])
        for place in locations:
            address = place.get("address") if isinstance(place, dict) else None
            text = _address_text(address) if isinstance(address, dict) else None
            if text:
                values.append(text)
    return list(dict.fromkeys(values))


def _detail_header(soup: BeautifulSoup) -> dict:
    section = soup.select_one(".job-description")
    wrapper = section.select_one(".jd-wrapper") if section else None
    scope = wrapper or section
    if scope is None:
        return {"category": None, "location": None, "job_id_text": None, "apply_urls": []}
    location = scope.select_one(".job-location")
    category = None
    for node in scope.select(".job-category, .job-date"):
        text = normalized_title(node.get_text(" ", strip=True))
        if text.lower().startswith("job category:"):
            category = text.split(":", 1)[1].strip()
        elif node.get("class") and "job-category" in node.get("class"):
            category = text or None
    job_id_text = scope.select_one(".job-id")
    apply_urls = []
    for anchor in soup.select("a.job-apply[href]"):
        href = anchor.get("href", "").strip()
        if href:
            apply_urls.append(href)
    return {
        "category": category,
        "location": normalized_title(location.get_text(" ", strip=True)) if location else None,
        "job_id_text": normalized_title(job_id_text.get_text(" ", strip=True)) if job_id_text else None,
        "apply_urls": list(dict.fromkeys(apply_urls)),
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading with the verbatim body after it."""
    root = BeautifulSoup(html or "", "lxml")
    body = root.select_one(".ats-description") or root
    for node in body.select("script,style,noscript,a.job-apply,button.js-save-job-btn,.job-description__buttons"):
        node.decompose()
    headings = body.select("h1,h2,h3,h4,h5,h6")
    heading_ids = {id(node) for node in headings}
    sections: list[dict] = []
    current = None
    for node in body.descendants:
        if isinstance(node, Tag):
            if node.name in HEADING_TAGS and id(node) in heading_ids:
                current = {"heading": normalized_title(node.get_text(" ", strip=True)), "text": ""}
                sections.append(current)
            continue
        if not isinstance(node, NavigableString) or id(node) in heading_ids:
            continue
        if node.find_parent(HEADING_TAGS):
            continue
        text = str(node)
        if not text.strip():
            continue
        if current is None:
            current = {"heading": None, "text": ""}
            sections.append(current)
        current["text"] = (current["text"] + " " + text).strip()
    for section in sections:
        section["text"] = normalized_title(section["text"])
    return [section for section in sections if section["heading"] or section["text"]]


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    """Parse an exact company-listed job page and its full rendered body."""
    parsed = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != BASE_HOST
            or match is None or match.group(3) != str(listing_row.get("id"))):
        raise ValueError("NetApp detail response is not the exact listed company job URL")

    soup = BeautifulSoup(html, "lxml")
    meta = _meta_map(soup)
    jobposting = _jobposting(soup)
    section = soup.select_one(".job-description")
    if section is None:
        raise ValueError("NetApp detail page has no job-description section")
    ats = section.select_one(".ats-description")
    if ats is None:
        raise ValueError("NetApp detail page has no rendered ats-description body")
    for node in ats.select("script,style,noscript,a.job-apply,button.js-save-job-btn,.job-description__buttons"):
        node.decompose()
    description_html = str(ats)
    description_text = normalized_visible_text(description_html)
    if len(description_text) < 100:
        raise ValueError("NetApp rendered job description is empty or too short")

    header = _detail_header(soup)
    h1_values = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    h1_values = [value for value in h1_values if value and "Explore roles" not in value]
    # Some imported descriptions contain section headings marked up as h1
    # ("About DataPelago and NetApp", "Key Success Measures").  The job title
    # is the first h1 after the site's generic marketing heading, not the last.
    visible_title = h1_values[0] if h1_values else ""
    listing_title = normalized_title(listing_row.get("title"))
    jsonld_title = normalized_title(jobposting.get("title")) if jobposting else ""
    meta_title = normalized_title(meta.get("job-tbcn-job-title"))
    title = jsonld_title or meta_title or listing_title or visible_title
    if not title:
        raise ValueError("NetApp detail page has no job title")
    issues = []
    for label, value in (("jsonld", jsonld_title), ("meta", meta_title),
                         ("listing", listing_title), ("h1", visible_title)):
        if value and normalized_title(value) != title:
            issues.append(f"netapp_title_conflict_{label}")

    listing_id = str(listing_row.get("id"))
    meta_id = (meta.get("search-analytics-currentJobId") or "").strip()
    if meta_id and meta_id != listing_id:
        raise ValueError("NetApp detail currentJobId does not match the listing card")
    requisition_id = _req_id(jobposting.get("identifier")) if jobposting else None
    meta_req = (meta.get("job-ats-req-id") or "").strip() or None
    if requisition_id and meta_req and requisition_id != meta_req:
        issues.append("netapp_requisition_conflict")
    requisition_id = requisition_id or meta_req
    if not requisition_id:
        issues.append("netapp_requisition_missing")

    date_raw = jobposting.get("datePosted") if jobposting else None
    date_posted = _iso_date(date_raw)
    if date_raw and date_posted is None:
        issues.append("netapp_datePosted_unreadable")
    date_updated_raw = jobposting.get("dateModified") if jobposting else None
    date_updated = _iso_date(date_updated_raw)
    valid_raw = jobposting.get("validThrough") if jobposting else None
    valid_through = _iso_date(valid_raw)
    if valid_raw and valid_through is None:
        issues.append("netapp_validThrough_unreadable")

    meta_apply = (meta.get("search-job-apply-url") or "").strip() or None
    apply_url = meta_apply or (header["apply_urls"][0] if header["apply_urls"] else None)
    if meta_apply and header["apply_urls"] and meta_apply not in header["apply_urls"]:
        issues.append("netapp_apply_url_conflict")

    locations = _locations(listing_row.get("location", ""), header["location"], jobposting)
    if not locations:
        issues.append("netapp_locations_missing")
    employer = None
    if jobposting and isinstance(jobposting.get("hiringOrganization"), dict):
        employer = jobposting["hiringOrganization"].get("name")
    sections = description_sections(description_html)
    responsibilities = [
        item for item in sections
        if item["heading"] and any(token in item["heading"].lower()
                                    for token in ("responsibilit", "what you will do", "what you'll do"))
    ]
    qualifications = [
        item for item in sections
        if item["heading"] and any(token in item["heading"].lower()
                                    for token in ("qualif", "requirement", "education", "experience",
                                                  "what you will need", "what you'll need", "who you are"))
    ]
    return {
        "job_id": listing_id,
        "title": title,
        "listing_title": listing_title,
        "requisition_id": requisition_id,
        "job_id_text": header["job_id_text"],
        "category": header["category"],
        "detail_location": header["location"],
        "locations": locations,
        "apply_url": apply_url,
        "apply_urls": header["apply_urls"],
        "date_posted": date_posted,
        "date_posted_raw": date_raw,
        "date_updated": date_updated,
        "date_updated_raw": date_updated_raw,
        "valid_through": valid_through,
        "valid_through_raw": valid_raw,
        "salary": _salary(jobposting.get("baseSalary")) if jobposting else None,
        "employment_type": jobposting.get("employmentType") if jobposting else None,
        "workplace_type": jobposting.get("jobLocationType") if jobposting else None,
        "employer": employer,
        "language": (jobposting.get("inLanguage") if jobposting else None)
                    or (soup.html.get("lang") if soup.html else None),
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "description_sha256": sha256(description_text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(description_text),
        "jsonld": jobposting,
        "meta": {key: meta[key] for key in (
            "search-analytics-currentJobId", "search-job-apply-url", "search-job-feed-id",
            "job-ats-req-id", "job-tbcn-job-title", "gtm_tbcn_jobcategory",
            "gtm_tbcn_location", "job-category-ids", "job-location-ids",
        ) if key in meta},
        "issues": issues,
    }


def _listing_metadata(value: dict) -> dict:
    """Keep page counts/IDs in metadata without repeating every card row."""
    result = {key: item for key, item in value.items() if key not in ("rows", "pages")}
    result["pages"] = [
        {key: item for key, item in page.items() if key != "rows"}
        for page in value.get("pages", [])
    ]
    return result


class NetApp(Adapter):
    """Collect the complete unfiltered NetApp global listing from its own site."""

    VERIFICATION_REVISION = "netapp.talentbrew-global-listing-and-native-pages.1"

    async def _listing_page(self, page: int) -> dict:
        if not self.page_guard():
            raise ValueError("NetApp listing page budget exhausted")
        url = OFFICIAL_ENTRY if page == 1 else f"{OFFICIAL_ENTRY}?p={page}"
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"NetApp listing page {page} redirected to {response.url}")
        value = parse_listing_page(response.text, str(response.url), page)
        value.update({
            "request_url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": round(seconds, 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return value

    async def _listing_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        first = await self._listing_page(1)
        total, total_pages = first["total"], first["total_pages"]
        pages = [first]
        rows = list(first["rows"])
        seen = set(first["job_ids"])
        issues = list(first["issues"])
        if total_pages < 1:
            raise ValueError("NetApp published no listing pages")
        for page in range(2, total_pages + 1):
            value = await self._listing_page(page)
            if value["total"] != total or value["total_pages"] != total_pages:
                issues.append(f"netapp_pagination_changed_on_page:{page}")
            issues.extend(value["issues"])
            for row in value["rows"]:
                if row["id"] in seen:
                    issues.append(f"netapp_duplicate_id_across_pages:{row['id']}")
                    continue
                seen.add(row["id"])
                rows.append(row)
            pages.append(value)
        expected_pages = math.ceil(total / ITEMS_PER_PAGE) if total else 0
        if total_pages != expected_pages:
            issues.append("netapp_listing_total_pages_mismatch")
        if len(rows) != total:
            issues.append("netapp_listing_total_does_not_match_retained_rows")
        return {
            "label": label,
            "total": total,
            "total_pages": total_pages,
            "items_per_page": ITEMS_PER_PAGE,
            "rows": rows,
            "job_ids": [row["id"] for row in rows],
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def detail(self, job) -> object:
        started = time.perf_counter()
        response = await self.http.get(job.url)
        parsed = parse_detail_page(response.text, str(response.url), job.raw["listing"])
        parsed["response"] = {
            "request_url": job.url,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        if parsed["job_id"] != str(job.job_id):
            raise ValueError("NetApp detail ID does not match the listing job")
        job.raw["detail"] = parsed
        job.title = parsed["title"]
        job.requisition_id = parsed["requisition_id"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "netapp.talentbrew.ats-description"
        job.description_source_url = str(response.url)
        job.locations = parsed["locations"]
        job.department = parsed["category"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.date_updated = parsed["date_updated"]
        job.valid_through = parsed["valid_through"]
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.employer = parsed["employer"]
        job.language = parsed["language"]
        for issue in parsed["issues"]:
            job.warnings.append(issue)
        return job

    def _make_job(self, row: dict):
        return self.job(
            {"listing": dict(row)},
            title=row["title"], url=row["url"], job_id=row["id"],
            description="", method=None, description_url=None,
            locations=[part.strip() for part in row.get("location", "").split(";") if part.strip()],
        )

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": "NetApp's own unfiltered global TalentBrew search page, all numbered pages.",
            "listing_request": {"method": "GET", "page_1": OFFICIAL_ENTRY,
                                "page_n": OFFICIAL_ENTRY + "?p=N",
                                "observed_items_per_page": ITEMS_PER_PAGE,
                                "filters_applied": False},
            "detail_method": "Exact company-listed detail URL; full rendered .ats-description after removing Apply/Save controls.",
        })
        try:
            initial = await self._listing_snapshot("initial")
        except Exception as exc:
            self.result.errors.append(f"NetApp initial listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["total"]
        self.result.metadata["initial_listing"] = _listing_metadata(initial)
        self.result.metadata["initial_job_ids"] = initial["job_ids"]
        for issue in initial["issues"]:
            self.result.errors.append(issue)

        jobs = [self._make_job(row) for row in initial["rows"]]
        self.result.jobs = jobs
        await self.hydrate(jobs, self.detail)

        final = None
        try:
            final = await self._listing_snapshot("final")
        except Exception as exc:
            self.result.errors.append(f"NetApp final listing recheck failed: {type(exc).__name__}: {exc}")
        reconciliation = {
            "initial_count": initial["total"],
            "initial_pages": initial["total_pages"],
            "initial_ids": initial["job_ids"],
            "final_count": final["total"] if final else None,
            "final_pages": final["total_pages"] if final else None,
            "final_ids": final["job_ids"] if final else None,
            "new_job_ids": ([i for i in final["job_ids"] if i not in set(initial["job_ids"])] if final else None),
            "removed_job_ids": ([i for i in initial["job_ids"] if i not in set(final["job_ids"])] if final else None),
            "same_ids_and_order": bool(final and initial["job_ids"] == final["job_ids"]),
            "same_count_and_pages": bool(final and (initial["total"], initial["total_pages"]) == (final["total"], final["total_pages"])),
            "final_issues": final["issues"] if final else ["final_listing_unavailable"],
            "complete": False,
        }
        changed_fields = {}
        if final:
            initial_by_id = {row["id"]: row for row in initial["rows"]}
            final_by_id = {row["id"]: row for row in final["rows"]}
            for identity in initial["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            self.result.metadata["final_listing"] = _listing_metadata(final)
            self.result.metadata["final_job_ids"] = final["job_ids"]
            if not reconciliation["same_ids_and_order"]:
                self.result.errors.append("netapp_listing_membership_changed_during_collection")
            if not reconciliation["same_count_and_pages"]:
                self.result.errors.append("netapp_listing_count_or_pages_changed_during_collection")
            for issue in final["issues"]:
                self.result.errors.append(issue)
        reconciliation["changed_fields_by_job_id"] = changed_fields
        if changed_fields:
            self.result.errors.append("netapp_listing_fields_changed_during_collection")
        reconciliation["complete"] = bool(
            final and reconciliation["same_ids_and_order"] and reconciliation["same_count_and_pages"]
            and not changed_fields and not initial["issues"] and not final["issues"]
        )
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["detail_hydration_mode"] = "One first-party detail request per listed job."
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["listing_pages"] = initial["total_pages"]
        self.result.metadata["listing_passes"] = 2
        self.result.listing_complete = bool(reconciliation["complete"] and len(jobs) == initial["total"])
        self.result.timings["initial_listing_seconds"] = initial["seconds"]
        if final:
            self.result.timings["final_listing_seconds"] = final["seconds"]
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
