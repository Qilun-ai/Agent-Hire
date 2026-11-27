"""Siemens EDA's own current careers listing on the Siemens DISW job portal.

Membership authority is the company-published ``Electronic Design Automation
(EDA)`` business-structure listing of jobs.sw.siemens.com (the careers site
reached from Siemens EDA's legacy official domain ``mentor.com/company/careers``
and from ``sw.siemens.com/en-US/careers``).  The listing's own search API is
paginated 10 rows at a time; every row is one posted requisition location and
carries its full description.  The exact job URL is the one rendered by the
company listing UI, derived from the row's own city/location/title slug fields.

The ledger entry ``eda.sw.siemens.com/en-US/careers`` now 301s to the EDA
marketing page and publishes no job list; that redirect is recorded, not used
as a zero-job substitute.  Only the exact EDA business-structure rows are
collected, never a Siemens-wide board or a keyword/aggregator search.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
import unicodedata

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://eda.sw.siemens.com/en-US/careers/"
OFFICIAL_LEGACY_ENTRY = "https://www.mentor.com/company/careers"
CAREERS_HOME = "https://jobs.sw.siemens.com/"
LISTING_URL = "https://jobs.sw.siemens.com/business-structure/electronic-design-automation-eda/jobs/"
SEARCH_API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
MICROSITE_DATA = "https://microsites.dejobs.org/jobs-sw-siemens-com/data/{guid}.json"
BUSINESS_STRUCTURE = "electronic-design-automation-eda"
PAGE_SIZE = 10
GUID = re.compile(r"^[0-9A-Fa-f]{32}$")
APPLY_LINK = re.compile(r"^https://(?:de|rr)\.jobsyn\.org/[0-9A-Fa-f]{32}(?:10)?/?$")

QUALIFICATION_HEADINGS = re.compile(
    r"qualif|requirement|what you(?:'|’)?(?:ll)? (?:need|bring)|skills|experience|education|"
    r"応募要件|必須|歓迎|学歴|言語力|スキル|経験",
    re.I,
)
RESPONSIBILITY_HEADINGS = re.compile(
    r"responsib|what you(?:'|’)?(?:ll)? do|role overview|your (?:tasks|role)|key tasks|"
    r"main (?:tasks|responsibilities)|duties|job description|業務内容|職務内容|仕事内容|役割",
    re.I,
)
NON_SECTION_HEADINGS = re.compile(
    r"job family|req id|organization|job type|category|#li-|transform the everyday|"
    r"working at|why us|benefits|about us|company overview|equal opportunity|eeo|"
    r"similar jobs|share",
    re.I,
)
RANGE = re.compile(
    r"(?:salary|pay|compensation|total cash compensation)\s+range[^.]{0,240}",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def content_normal_form(value: str, *, markdown: bool = False) -> str:
    """Compare the listing's inline markdown with the detail page's rendered body."""
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("\\#", "#")
    value = re.sub(r"\\([*_#>`~-])", r"\1", value)
    value = re.sub(r"[*_#>`~]", "", value)
    if markdown:
        value = "\n".join(
            re.sub(r"^\s*(?:[+*-]|\d+\.)\s+", "", line) for line in value.splitlines()
        )
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def job_url(row: dict) -> str:
    """Rebuild the exact href published by the listing UI for this row."""
    slab = (row.get("city_slab_exact") or "").split("/jobs::")[0].split("/")
    city = slab[0] if slab and slab[0] else (row.get("city_slug") or "none")
    location = row.get("location_exact") or ""
    if city == "global" or location.casefold() == "global":
        path = "global"
    else:
        short = location.rsplit(",", 1)[-1].strip().lower() if "," in location else ""
        path = f"{city}-{short}" if short else city
    title_slug = row.get("title_slug") or ""
    identity = row.get("guid") or ""
    if not title_slug or not GUID.fullmatch(identity):
        raise ValueError("Siemens EDA listing row lacks a usable title slug or GUID")
    return f"https://jobs.sw.siemens.com/{path}/{title_slug}/{identity}/job/"


def work_type(row: dict) -> str | None:
    raw = row.get("other")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    value = value.get("work_type") if isinstance(value, dict) else None
    return normalized_title(value) if isinstance(value, str) and value.strip() else None


def parse_listing_page(payload: dict, requested_page: int) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Siemens EDA search API response is not an object")
    pagination = payload.get("pagination")
    jobs = payload.get("jobs")
    if not isinstance(pagination, dict) or not isinstance(jobs, list):
        raise ValueError("Siemens EDA search API response is missing jobs or pagination")
    total, pages, page, size = (pagination.get(key) for key in ("total", "total_pages", "page", "page_size"))
    if (not isinstance(total, int) or not isinstance(pages, int) or not isinstance(page, (int, float))
            or int(page) != requested_page or size != PAGE_SIZE or pages < 1
            or len(jobs) > PAGE_SIZE):
        raise ValueError("Siemens EDA search API pagination schema changed")
    expected = min(PAGE_SIZE, max(0, total - (requested_page - 1) * PAGE_SIZE))
    rows, issues, seen = [], [], set()
    for row in jobs:
        if not isinstance(row, dict):
            issues.append("row_not_object")
            continue
        guid = str(row.get("guid") or "")
        if not GUID.fullmatch(guid):
            issues.append(f"row_missing_guid:{guid[:16]}")
            continue
        if guid in seen:
            issues.append(f"duplicate_guid:{guid}")
            continue
        seen.add(guid)
        if row.get("company") not in (None, "Siemens"):
            issues.append(f"row_wrong_company:{guid}")
        if not normalized_title(row.get("title_exact")) or not row.get("title_slug"):
            issues.append(f"row_missing_title:{guid}")
        if not row.get("reqid"):
            issues.append(f"row_missing_reqid:{guid}")
        description = row.get("description")
        if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
            issues.append(f"row_missing_description:{guid}")
        for key in ("date_added", "date_new", "date_updated", "city_slab_exact", "location_exact"):
            if not row.get(key):
                issues.append(f"row_missing_{key}:{guid}")
        if work_type(row) is None:
            issues.append(f"row_missing_work_type:{guid}")
        rows.append(dict(row))
    if len(jobs) != expected:
        issues.append(f"page_row_count_mismatch:{len(jobs)}!={expected}")
    return {"rows": rows, "total": total, "pages": pages, "page": int(page), "page_size": size, "issues": issues}


def parse_detail_payload(payload: dict, *, guid: str, title: str, reqid: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Siemens EDA microsite detail payload is not an object")
    if str(payload.get("guid") or "").upper() != guid.upper():
        raise ValueError("Siemens EDA detail payload GUID differs from the listed job")
    if normalized_title(payload.get("title")) != normalized_title(title):
        raise ValueError(
            f"Siemens EDA title conflict listing={title!r} detail={payload.get('title')!r}"
        )
    if str(payload.get("reqid") or "") != str(reqid):
        raise ValueError("Siemens EDA detail requisition differs from the listed job")
    html = payload.get("html_description")
    markdown = payload.get("text")
    if not isinstance(html, str) or len(normalized_body_text(html)) < 100:
        raise ValueError("Siemens EDA detail payload has no full html_description")
    if not isinstance(markdown, str) or len(normalized_body_text(markdown)) < 100:
        raise ValueError("Siemens EDA detail payload has no full text description")
    link = payload.get("link")
    if not isinstance(link, str) or not APPLY_LINK.fullmatch(link):
        raise ValueError("Siemens EDA detail payload has an unexpected apply link")
    if payload.get("company") not in (None, "Siemens"):
        raise ValueError("Siemens EDA detail payload company changed")
    return dict(payload)


def footer_field(html: str, label: str) -> str | None:
    pattern = re.compile(rf"<strong>\s*{re.escape(label)}\s*:\s*</strong>\s*([^<]+)", re.I)
    match = pattern.search(html or "")
    if match:
        value = normalized_title(BeautifulSoup(match.group(1), "lxml").get_text(" ", strip=True))
        return value or None
    text = normalized_body_text(html)
    match = re.search(
        rf"(?:^|\s){re.escape(label)}:\s*([^|#]+?)(?=\s+(?:Organization|Job Type|Category):|$)",
        text,
        re.I,
    )
    return normalized_title(match.group(1)) if match else None


CURRENCY = r"(?:USD|EUR|GBP|CAD|CHF|JPY|INR|CNY|AUD|SGD|KRW|TWD|ILS|PLN|AMD|[$€£¥₹₪])"
RANGE_PAIR = re.compile(
    rf"(?:(?P<pre>{CURRENCY})\s*)?(?P<min>\d[\d,]*)\s*(?:-|–|—|~|〜|to)\s*"
    rf"(?:(?P<post>{CURRENCY})\s*)?(?P<max>\d[\d,]*)",
    re.I,
)
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₪": "ILS"}
JP_SALARY = re.compile(r"年俸[^。\n]{0,40}?([\d,]+)\s*[～〜~-]\s*([\d,]+)\s*万円")


def _currency(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value in SYMBOLS:
        return SYMBOLS[value]
    return value.upper()


def salary_from_text(text: str) -> dict | None:
    value = text or ""
    jp = JP_SALARY.search(value)
    if jp:
        raw = normalized_title(jp.group(0))
        low, high = (int(item.replace(",", "")) * 10000 for item in jp.groups())
        return {
            "raw": raw, "currency": "JPY", "min": min(low, high), "max": max(low, high),
            "unit": "万円", "source": "job_description",
        }
    match = RANGE.search(value)
    if not match:
        return None
    raw = normalized_title(match.group(0)).rstrip(" ;,")
    if not re.search(r"\d", raw):
        return None
    cleaned = re.sub(r"(?<=,)\s+(?=\d)", "", raw)
    pairs = []
    for item in RANGE_PAIR.finditer(cleaned):
        currency = _currency(item.group("pre") or item.group("post"))
        low = int(item.group("min").replace(",", ""))
        high = int(item.group("max").replace(",", ""))
        if low > high:
            low, high = high, low
        pairs.append({"currency": currency, "min": low, "max": high})
    # A preceding currency may only be present before the low amount.
    pairs = [item for item in pairs if not (item["max"] < 1000 and item["min"] < 1000)]
    if not pairs:
        return None
    currencies = {item["currency"] for item in pairs if item["currency"]}
    record = {"raw": raw, "currency": None, "min": None, "max": None, "source": "job_description"}
    if len(pairs) == 1:
        record.update(currency=pairs[0]["currency"], min=pairs[0]["min"], max=pairs[0]["max"])
    elif len(currencies) <= 1:
        currency = currencies.pop()
        record.update(
            currency=currency,
            min=min(item["min"] for item in pairs),
            max=max(item["max"] for item in pairs),
            ranges=pairs,
        )
    else:
        record["ranges"] = pairs
    return record


def html_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.body or soup
    sections, current = [], None
    for child in root.find_all(recursive=False):
        name = (child.name or "").lower()
        text = normalized_title(child.get_text(" ", strip=True))
        if not text:
            continue
        heading = None
        if name in ("h1", "h2", "h3", "h4"):
            heading = text
        elif name == "p":
            strong = child.find("strong")
            if strong:
                strong_text = normalized_title(strong.get_text(" ", strip=True)).rstrip(":").strip()
                if len(text) <= 90 and strong_text == text.rstrip(":").strip():
                    heading = text
            elif len(text) <= 60 and text[0] in "■●【":
                heading = text
        if heading:
            current = {"heading": heading.rstrip(":").strip(), "text": []}
            sections.append(current)
        elif current is not None:
            current["text"].append(text)
    return [
        {"heading": item["heading"], "text": normalized_title(" ".join(item["text"]))}
        for item in sections
        if item["text"]
    ]


def _join_sections(items: list[dict]) -> str | None:
    values = [f"{item['heading']}\n{item['text']}" for item in items if len(item["text"]) >= 40]
    return "\n\n".join(values) if values else None


def sections_from_html(html: str) -> tuple[list[dict], str | None, str | None]:
    sections = html_sections(html)
    qualifications = [
        item for item in sections
        if QUALIFICATION_HEADINGS.search(item["heading"])
        and not NON_SECTION_HEADINGS.search(item["heading"])
    ]
    responsibilities = [
        item for item in sections
        if RESPONSIBILITY_HEADINGS.search(item["heading"])
        and not NON_SECTION_HEADINGS.search(item["heading"])
    ]
    return sections, _join_sections(qualifications), _join_sections(responsibilities)


def markdown_to_html(value: str) -> str:
    """Small fallback renderer for the listing's inline markdown description."""
    lines = (value or "").replace("\\#", "#").splitlines()
    parts, paragraph, items = [], [], []

    def flush():
        nonlocal paragraph, items
        if paragraph:
            text = " ".join(paragraph)
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
            parts.append(f"<p>{text}</p>")
            paragraph = []
        if items:
            rendered = "".join(
                "<li>" + re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item) + "</li>"
                for item in items
            )
            parts.append(f"<ul>{rendered}</ul>")
            items = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if re.match(r"^[+*-]\s+", stripped):
            if paragraph:
                flush()
            items.append(re.sub(r"^[+*-]\s+", "", stripped))
        elif re.match(r"^\d+\.\s+", stripped):
            if paragraph:
                flush()
            items.append(re.sub(r"^\d+\.\s+", "", stripped))
        else:
            paragraph.append(stripped)
    flush()
    return "\n".join(parts)


class SiemensEda(Adapter):
    """Collect the company-published EDA business-structure listing only."""

    VERIFICATION_REVISION = "siemens-eda.disw-eda-business-structure.1"

    def _listing_params(self, page: int) -> dict:
        return {"page": page, "businessStructures": BUSINESS_STRUCTURE, "num_items": PAGE_SIZE}

    async def _listing_page(self, page: int) -> dict:
        if not self.page_guard():
            raise ValueError("Siemens EDA page budget exhausted")
        payload = await self.http.json(
            SEARCH_API, params=self._listing_params(page),
            headers={"x-origin": "jobs.sw.siemens.com", "Accept": "application/json"},
        )
        return parse_listing_page(payload, page)

    async def detail(self, job):
        source_url = MICROSITE_DATA.format(guid=job.job_id)
        try:
            response = await self.http.request(
                "GET", source_url, headers={"Accept": "application/json"}
            )
            payload = response.json()
            detail = parse_detail_payload(
                payload, guid=job.job_id, title=job.title, reqid=job.requisition_id
            )
        except Exception as exc:
            job.warnings.append("detail_fetch_failed")
            job.description_html = markdown_to_html(job.raw["listing"].get("description") or "")
            job.description_text = normalized_title(job.raw["listing"].get("description") or "")
            job.description_method = "listing_api_inline_markdown_fallback"
            job.description_source_url = SEARCH_API
            job.raw["detail_error"] = f"{type(exc).__name__}: {exc}"
            return job
        digest = sha256(response.content).hexdigest()
        listing = job.raw["listing"]
        full_text = text_html(detail["html_description"])
        sections, qualifications, responsibilities = sections_from_html(detail["html_description"])
        job.description_html = detail["html_description"]
        job.description_text = full_text
        job.description_method = "company_microsite_html_description"
        job.description_source_url = source_url
        job.apply_url = detail["link"]
        job.employer = detail.get("job_source_name") or "Siemens Digital Industries Software"
        job.employment_type = footer_field(detail["html_description"], "Job Type")
        job.salary = salary_from_text(full_text)
        job.qualifications = qualifications
        job.responsibilities = responsibilities
        job.date_posted = None
        job.date_updated = None
        job.date_posted_raw = {
            "date_added": detail.get("date_added"),
            "date_new": detail.get("date_new"),
            "date_updated": detail.get("date_updated"),
            "jsonld_datePosted_basis": "date_added",
            "note": (
                "The site's JSON-LD datePosted uses the microsite index date "
                "(date_added); date_new/date_updated are the source record timestamps. "
                "None is a verified employer posting date, so date_posted stays null."
            ),
        }
        job.raw["detail"] = detail
        job.raw["detail"]["response_sha256"] = digest
        job.raw["source_ui"] = {
            "listing_url": LISTING_URL,
            "listing_job_url": job.url,
            "apply_link_published": detail["link"],
            "organization": footer_field(detail["html_description"], "Organization"),
            "category": footer_field(detail["html_description"], "Category"),
            "work_type": work_type(listing),
            "sections": sections,
            "salary": job.salary,
            "listing_description_sha256": sha256(listing["description"].encode()).hexdigest(),
            "detail_text_sha256": sha256(detail["text"].encode()).hexdigest(),
            "listing_description_matches_detail_body": (
                content_normal_form(listing["description"], markdown=True)
                == content_normal_form(normalized_body_text(detail["html_description"]))
            ),
            "rendered_body_has_internal_hashtags": bool(re.search(r"#LI-[A-Za-z0-9]+", detail["html_description"])),
        }
        return job

    async def run(self):
        listing_started = time.perf_counter()
        first = await self._listing_page(1)
        total, pages = first["total"], first["pages"]
        rows = list(first["rows"])
        ids = [row["guid"] for row in rows]
        issues = list(first["issues"])
        for page in range(2, pages + 1):
            parsed = await self._listing_page(page)
            if parsed["total"] != total or parsed["pages"] != pages:
                issues.append("listing_pagination_changed_during_collection")
                total, pages = parsed["total"], parsed["pages"]
            ids.extend(row["guid"] for row in parsed["rows"])
            rows.extend(parsed["rows"])
            issues.extend(parsed["issues"])
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.expected_total = total
        self.result.metadata["listing"] = {
            "url": LISTING_URL,
            "search_api": SEARCH_API,
            "business_structure": BUSINESS_STRUCTURE,
            "page_size": PAGE_SIZE,
            "reported_total": total,
            "reported_pages": pages,
            "rows_fetched": len(rows),
            "unique_ids": len(set(ids)),
            "issues": issues,
        }
        if len(rows) != total or len(set(ids)) != total:
            self.result.errors.append(
                f"siemens_eda_listing_rows_{len(rows)}_unique_{len(set(ids))}_reported_{total}"
            )
        if issues:
            self.result.errors.extend(f"siemens_eda_{issue}" for issue in issues)

        for row in rows:
            try:
                url = job_url(row)
            except ValueError as exc:
                self.result.errors.append(f"{row.get('guid')}: {exc}")
                continue
            identity = str(row.get("guid"))
            self.add(self.job(
                {"listing": row},
                title=normalized_title(row.get("title_exact")),
                url=url,
                job_id=identity,
                requisition_id=str(row.get("reqid") or "") or None,
                locations=[row.get("location_exact")] if row.get("location_exact") else [],
                department=row.get("job_category"),
                workplace_type=work_type(row),
                apply_url=None,
                employer="Siemens Digital Industries Software",
                date_posted=None,
                date_posted_raw={
                    "date_added": row.get("date_added"),
                    "date_new": row.get("date_new"),
                    "date_updated": row.get("date_updated"),
                    "note": "No reliable employer posting date in the listing; see detail note.",
                },
            ))

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        final_rows, final_issues = [], []
        final_total = None
        for page in range(1, pages + 1):
            parsed = await self._listing_page(page)
            if final_total is None:
                final_total = parsed["total"]
            final_rows.extend(parsed["rows"])
            final_issues.extend(parsed["issues"])
        final_seconds = max(0.0, time.perf_counter() - final_started)
        final_ids = [row["guid"] for row in final_rows]
        initial_by_id = {row["guid"]: row for row in rows}
        final_by_id = {row["guid"]: row for row in final_rows}
        stable_fields = (
            "guid", "reqid", "title_exact", "title_slug", "description", "location_exact",
            "city_slab_exact", "job_category", "other", "date_new", "date_updated",
            "company", "buid",
        )
        changed_fields = {}
        for guid in sorted(set(initial_by_id) & set(final_by_id)):
            diffs = [
                key for key in stable_fields
                if initial_by_id[guid].get(key) != final_by_id[guid].get(key)
            ]
            if diffs:
                changed_fields[guid] = diffs
        changed = sorted(changed_fields)
        new_ids = sorted(set(final_by_id) - set(initial_by_id))
        removed_ids = sorted(set(initial_by_id) - set(final_by_id))
        complete = (
            not self.result.errors
            and final_total == total == len(self.result.jobs) == len(rows)
            and final_ids == ids
            and not changed and not new_ids and not removed_ids and not final_issues
        )
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": ids,
            "final_ids": final_ids,
            "initial_final_id_order_matches": final_ids == ids,
            "initial_final_full_rows_match": not changed and not new_ids and not removed_ids,
            "changed_fields_by_id": changed_fields,
            "volatile_fields_ignored": ["score", "_version_", "salted_date", "date_added"],
            "new_ids": new_ids,
            "removed_ids": removed_ids,
            "initial_total": total,
            "final_total": final_total,
            "complete": complete,
        }
        if not complete:
            if not any("listing" in error or "membership" in error for error in self.result.errors):
                self.result.errors.append("siemens_eda_listing_changed_during_collection")
        self.result.listing_complete = complete
        self.result.timings["listing_seconds"] = listing_seconds
        self.result.timings["detail_network_seconds"] = detail_seconds
        self.result.timings["final_recheck_seconds"] = final_seconds
        self.result.metadata["listing"]["collected_jobs"] = len(self.result.jobs)
        return self.result
