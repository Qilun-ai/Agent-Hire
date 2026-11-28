"""Electronic Arts's own careers journey: EA official page -> jobs.ea.com Avature portal.

Observed flow (2026-11-07):
- ``https://www.ea.com/careers`` publishes calls-to-action to ``https://jobs.ea.com``.
- ``https://jobs.ea.com`` lands on the Avature listing ``https://jobs.ea.com/en_US/careers``
  (server-rendered, ``article.article--result`` rows, ``1-20 of 342 results`` legend).
- Pagination is ``/en_US/careers/Home/?jobRecordsPerPage=20&jobOffset=N`` (20/page).
- Each row links to an exact ``/en_US/careers/JobDetail/<slug>/<Role ID>`` page;
  the numeric Role ID is authoritative (the slug is ignored by the server).
- Detail pages expose ``General Information`` (Locations dataset(s), Role ID,
  Worker Type, Studio/Department, Work Model) and a ``Description & Requirements``
  article holding the full description; the Apply link is
  ``/en_US/careers/ApplicationMethods?jobId=<Role ID>``.

No facet (Country / Job Category / Worker Type / Studio/Department / Work Model),
keyword, or sort filter is applied, and the decorative RSS feed
(``/Home/feed/``, which ignores ``jobOffset`` and diverges from the HTML UI)
is not used as membership. No Avature/ATS board is enumerated: only the exact
listing hrefs published by the official portal are fetched.
"""
import hashlib
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

PAGE_SIZE = 20
PORTAL_PAGE = "https://jobs.ea.com/en_US/careers"
HOME_PAGE = "https://jobs.ea.com/en_US/careers/Home/"
DETAIL_ROUTE = re.compile(
    r"^/en_US/careers/JobDetail/[A-Za-z0-9][A-Za-z0-9\-]*/(\d+)$"
)
APPLY_ROUTE = re.compile(r"^/en_US/careers/ApplicationMethods$")
ROLE_ID = re.compile(r"^\d+$")
LEGEND_TOTAL = re.compile(r"of\s+(\d+)\s+results")
LEGEND_RANGE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+results")

SECTION_ENDERS = {
    "Key Responsibilities", "Required Qualifications", "Preferred Qualifications",
    "Pay Transparency - North America", "COMPENSATION AND BENEFITS", "PAY RANGES",
    "About Electronic Arts", "What you'll do:", "What you'll bring:",
}
PAY_END_PREFIX = "Pay is just one part of the overall compensation"


def compact_body_hash(html):
    """NFC/whitespace-free body hash, for cross-checks against retained HTML."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def listing_page_url(offset):
    if offset < 0 or offset % PAGE_SIZE:
        raise ValueError(f"EA listing offset must be a non-negative multiple of {PAGE_SIZE}")
    return f"{HOME_PAGE}?jobRecordsPerPage={PAGE_SIZE}&jobOffset={offset}"


def detail_job_id(url):
    """Numeric Role ID from an exact portal JobDetail href."""
    parts = urlsplit(str(url))
    if parts.scheme != "https" or parts.hostname != "jobs.ea.com":
        raise ValueError(f"EA listing exposes a non-portal detail URL: {url}")
    match = DETAIL_ROUTE.fullmatch(parts.path.rstrip("/"))
    if not match:
        raise ValueError(f"EA listing detail URL is not an exact JobDetail route: {url}")
    if parts.query or parts.fragment:
        raise ValueError(f"EA listing detail URL unexpectedly carries parameters: {url}")
    return match.group(1)


def _salary_from_description(description_text):
    """Retain the published PAY RANGES block verbatim; None when absent."""
    lines = [line.strip() for line in (description_text or "").splitlines()]
    if "PAY RANGES" not in lines:
        return None
    start = lines.index("PAY RANGES") + 1
    block = []
    for line in lines[start:]:
        if line.startswith(PAY_END_PREFIX):
            break
        if line:
            block.append(line)
    if not block:
        return None
    return {
        "source_text": "\n".join(block),
        "source_field": "description_text.PAY RANGES",
        "semantics": "Pay-transparency ranges as published in the description; preserve source text without conversion.",
    }


def _section_after_heading(description_text, heading, enders):
    """Lines following an exact heading line, until the next known heading."""
    lines = [line.strip() for line in (description_text or "").splitlines()]
    if heading not in lines:
        return None
    start = lines.index(heading) + 1
    kept = []
    for line in lines[start:]:
        if line in enders and line != heading:
            break
        if line:
            kept.append(line)
    return "\n".join(kept) if kept else None


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def parse_locations_field(field):
    """Parse a detail ``Locations`` field into (summary, structured datasets).

    Two published shapes exist: a summary line plus one or more
    ``ul.MultipleDataSetFields`` datasets (each ``Location``/``State``/``Country``
    triple is one workplace), or a bare summary string with no dataset.
    Raises ValueError on an unexpected shape.
    """
    value_el = field.select_one(".article__content__view__field__value")
    if value_el is None:
        raise ValueError("EA detail Locations field has no value element")
    datasets = []
    for ul in value_el.select("ul.MultipleDataSetFields"):
        items = ul.select("li.MultipleDataSetField")
        if not items:
            # The portal sometimes renders an empty dataset shell between
            # populated ones (e.g. Role ID 211550). It carries no field
            # values, so it is skipped rather than failing the detail.
            continue
        entry = {}
        for item in items:
            label_el = item.select_one(".MultipleDataSetFieldLabel")
            value = item.select_one(".MultipleDataSetFieldValue")
            label = label_el.get_text(" ", strip=True).rstrip(":").strip() if label_el else ""
            text = value.get_text(" ", strip=True) if value is not None else ""
            if not label:
                raise ValueError("EA detail Locations dataset has an unlabeled entry")
            entry[label] = text
        if not entry:
            raise ValueError("EA detail Locations dataset is empty")
        datasets.append(entry)
    summary_soup = BeautifulSoup(str(value_el), "lxml")
    for ul in summary_soup.select("ul.MultipleDataSetFields"):
        ul.decompose()
    summary = re.sub(r"\s+", " ", summary_soup.get_text(" ", strip=True)).strip()
    summary = re.sub(r"^Locations\s*:\s*", "", summary)
    if not summary and not datasets:
        raise ValueError("EA detail Locations field is empty")
    return summary or None, datasets


def _general_field_values(article):
    """Label -> value-element for a detail ``article--details`` block."""
    fields = {}
    for field in article.select(".article__content__view__field"):
        label_el = field.select_one(".article__content__view__field__label")
        if not label_el:
            continue
        label = label_el.get_text(" ", strip=True)
        if not label or label in fields:
            raise ValueError(f"EA detail General Information repeats field {label!r}")
        fields[label] = field
    return fields


def _detail_apply_url(soup, role_id):
    candidates = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        parts = urlsplit(href)
        if parts.hostname != "jobs.ea.com" or parts.path.rstrip("/") != "/en_US/careers/ApplicationMethods":
            continue
        job_ids = parse_qs(parts.query).get("jobId", [])
        if len(job_ids) != 1:
            continue
        candidates.append((anchor.get_text(" ", strip=True), href, job_ids[0]))
    exact = [href for _text, href, identity in candidates if identity == role_id]
    if len(exact) != 1:
        raise ValueError(
            f"EA detail page does not expose exactly one Apply link for Role ID {role_id}")
    return exact[0]


def parse_listing_page(html, page_url):
    """Parse one listing page into (legend_range, legend_total, rows)."""
    soup = BeautifulSoup(html, "lxml")
    legend_el = soup.select_one(".list-controls__text__legend")
    legend_text = re.sub(r"\s+", " ", legend_el.get_text(" ", strip=True)) if legend_el else ""
    legend_label = legend_el.get("aria-label", "") if legend_el else ""
    total_match = LEGEND_TOTAL.search(legend_text) or re.search(r"(\d+)\s+results", legend_label)
    if not total_match:
        raise ValueError(f"EA listing page {page_url} has no publishable results total")
    total = int(total_match.group(1))
    range_match = LEGEND_RANGE.match(legend_text)
    page_range = (int(range_match.group(1)), int(range_match.group(2))) if range_match else None
    rows = []
    for article in soup.select("article.article--result"):
        link = article.select_one("a.link_result[href]")
        if link is None:
            raise ValueError(f"EA listing page {page_url} has a result card without a title link")
        href = link.get("href", "").strip()
        identity = detail_job_id(urljoin(page_url, href))
        subtitle_el = article.select_one(".article__header__text__subtitle")
        spans = []
        if subtitle_el is not None:
            for span in subtitle_el.select("span"):
                spans.append({
                    "class": " ".join(span.get("class") or []),
                    "text": span.get_text(" ", strip=True),
                })
        by_class = {}
        for span in spans:
            for token in span["class"].split():
                by_class.setdefault(token, []).append(span["text"])
        id_texts = by_class.get("list-item-id", [])
        if len(id_texts) != 1 or id_texts[0] != f"Role ID {identity}":
            raise ValueError(
                f"EA listing page {page_url} job card ID text does not match its href: {identity}")
        rows.append({
            "job_id": identity,
            "title": link.get_text(" ", strip=True),
            "href": urljoin(page_url, href),
            "subtitle_spans": spans,
            "listing_location": (by_class.get("list-item-location", [None]) or [None])[0],
            "listing_job_posting_location": (by_class.get("list-item-jobPostingLocation", [None]) or [None])[0],
            "listing_worker_type": (by_class.get("list-item-workerType", [None]) or [None])[0],
            "listing_department": (by_class.get("list-item-department", [None]) or [None])[0],
        })
    if not rows:
        raise ValueError(f"EA listing page {page_url} shows zero job cards")
    ids = [row["job_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"EA listing page {page_url} repeats a Role ID")
    if any(not row["title"] for row in rows):
        raise ValueError(f"EA listing page {page_url} has an unidentified job card")
    return page_range, total, rows


def parse_detail_page(html, detail_url, listing_row):
    """Parse one exact JobDetail page against its listing row."""
    soup = BeautifulSoup(html, "lxml")
    articles = soup.select("article.article--details")
    if not articles:
        raise ValueError(f"EA detail page {detail_url} has no detail articles")
    headers = {}
    for index, article in enumerate(articles):
        header_el = article.select_one(".article__header__text__title")
        header = header_el.get_text(" ", strip=True) if header_el else None
        headers.setdefault(header, []).append(index)
    general = None
    description_article = None
    for article in articles:
        header_el = article.select_one(".article__header__text__title")
        header = header_el.get_text(" ", strip=True) if header_el else None
        if header == "General Information":
            general = article
        elif header == "Description & Requirements":
            description_article = article
    if general is None or description_article is None:
        raise ValueError(
            f"EA detail page {detail_url} is missing General Information or "
            "Description & Requirements")
    fields = _general_field_values(general)
    for required in ("Role ID", "Worker Type", "Studio/Department", "Work Model"):
        if required not in fields:
            raise ValueError(f"EA detail page {detail_url} General Information lacks {required!r}")
    role_el = fields["Role ID"].select_one(".article__content__view__field__value")
    role_id = role_el.get_text(" ", strip=True) if role_el is not None else ""
    if not ROLE_ID.fullmatch(role_id):
        raise ValueError(f"EA detail page {detail_url} Role ID is not numeric: {role_id!r}")
    if role_id != listing_row["job_id"]:
        raise ValueError(
            f"EA detail page {detail_url} Role ID {role_id} does not match its "
            f"listing href ID {listing_row['job_id']}")
    title_el = soup.select_one("h2")
    title = title_el.get_text(" ", strip=True) if title_el is not None else ""

    locations_summary = None
    locations = []
    locations_field = general.select_one(".article__content__view__field.field--locations")
    if locations_field is None:
        for candidate in general.select(".article__content__view__field"):
            value_el = candidate.select_one(".article__content__view__field__value")
            strong = value_el.select_one("strong") if value_el is not None else None
            if strong is not None and strong.get_text(" ", strip=True) == "Locations":
                locations_field = candidate
                break
    if locations_field is not None:
        locations_summary, datasets = parse_locations_field(locations_field)
        locations = [{"summary": locations_summary, **dataset} for dataset in datasets]
        if not datasets and locations_summary:
            locations = [{"summary": locations_summary}]
    content_el = description_article.select_one(".article__content")
    if content_el is None:
        raise ValueError(f"EA detail page {detail_url} Description article has no content block")
    description_html = content_el.decode_contents()
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"EA detail page {detail_url} Description & Requirements is implausibly short")
    apply_url = _detail_apply_url(soup, role_id)
    detail_metadata = {}
    for label, field in fields.items():
        value_el = field.select_one(".article__content__view__field__value")
        detail_metadata[label] = value_el.get_text(" ", strip=True) if value_el is not None else ""
    # Hidden/internal fields (Post To, LinkedInID) live in a header-less third
    # article on the portal; retain them in raw provenance only.
    for article in articles:
        if article is general or article is description_article:
            continue
        for field in article.select(".article__content__view__field"):
            label_el = field.select_one(".article__content__view__field__label")
            value_el = field.select_one(".article__content__view__field__value")
            if label_el:
                label = label_el.get_text(" ", strip=True)
                if label and label not in detail_metadata:
                    detail_metadata[label] = (
                        value_el.get_text(" ", strip=True) if value_el is not None else "")
    salary = _salary_from_description(description_text)
    return {
        "role_id": role_id,
        "title": title,
        "detail_url": detail_url,
        "locations_summary": locations_summary,
        "locations": locations,
        "worker_type": detail_metadata.get("Worker Type"),
        "department": detail_metadata.get("Studio/Department"),
        "work_model": detail_metadata.get("Work Model"),
        "detail_metadata": detail_metadata,
        "description_html": description_html,
        "description_text": description_text,
        "apply_url": apply_url,
        "salary": salary,
        "responsibilities": _section_after_heading(
            description_text, "Key Responsibilities", SECTION_ENDERS),
        "qualifications": _section_after_heading(
            description_text, "Required Qualifications", SECTION_ENDERS),
        "attachment_links": _job_attachment_links(description_html),
    }


class Ea(Adapter):
    """Follow EA's official entry into its Avature listing and exact JobDetail pages."""

    VERIFICATION_REVISION = "ea.avature-careers-home-listing-and-jobdetail.1"
    PAGE_SIZE = PAGE_SIZE
    PORTAL_PAGE = PORTAL_PAGE

    @staticmethod
    def _check_url(url, *, hosts, path_prefixes):
        parts = urlsplit(str(url))
        if parts.scheme != "https" or parts.hostname not in hosts:
            raise ValueError(f"EA flow left its official HTTPS hosts: {url}")
        if not any(parts.path.startswith(prefix) for prefix in path_prefixes):
            raise ValueError(f"EA flow requested an unexpected path: {url}")
        return parts

    async def _listing_page(self, url, offset):
        if not self.page_guard():
            raise ValueError("page_budget_exhausted")
        response = await self.http.get(url)
        self._check_url(response.url, hosts={"jobs.ea.com"},
                        path_prefixes=("/en_US/careers",))
        page_range, total, rows = parse_listing_page(response.text, str(response.url))
        expected_start = offset + 1
        expected_end = min(offset + PAGE_SIZE, total)
        if page_range != (expected_start, expected_end):
            raise ValueError(
                f"EA listing page offset {offset} legend {page_range} does not match "
                f"expected ({expected_start}, {expected_end})")
        if len(rows) != expected_end - expected_start + 1:
            raise ValueError(
                f"EA listing page offset {offset} holds {len(rows)} cards, "
                f"expected {expected_end - expected_start + 1}")
        return {
            "url": str(response.url), "offset": offset, "legend_range": list(page_range),
            "expected_total": total, "rows": rows, "ids": [row["job_id"] for row in rows],
        }

    async def _listing_sweep(self, *, first_url, label):
        """Fetch every listing page once; return (pages, rows, total)."""
        pages = []
        first = await self._listing_page(first_url, 0)
        pages.append(first)
        total = first["expected_total"]
        for offset in range(PAGE_SIZE, total, PAGE_SIZE):
            page = await self._listing_page(listing_page_url(offset), offset)
            if page["expected_total"] != total:
                raise ValueError(
                    f"EA {label} listing total changed between pages: "
                    f"{total} -> {page['expected_total']}")
            pages.append(page)
        rows = [row for page in pages for row in page["rows"]]
        ids = [row["job_id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"EA {label} listing pages repeat a Role ID")
        if len(rows) != total:
            raise ValueError(
                f"EA {label} listing reconciliation failed: "
                f"unique={len(ids)} expected={total}")
        return pages, rows, total

    def _make_job(self, row, parsed):
        role_id = parsed["role_id"]
        raw = {
            "listing": row,
            "detail_metadata": parsed["detail_metadata"],
            "source_ui": {
                "portal_page": self.PORTAL_PAGE,
                "listing_page_url_template": listing_page_url(0),
                "detail_url": parsed["detail_url"],
                "rendered_apply_url": parsed["apply_url"],
                "attachment_links": parsed["attachment_links"],
                "salary_from_description": parsed["salary"],
            },
        }
        locations = parsed["locations"] or (
            [{"summary": row["listing_location"]}] if row.get("listing_location") else [])
        job = self.job(
            raw,
            title=parsed["title"] or row["title"],
            url=parsed["detail_url"],
            job_id=role_id,
            requisition_id=role_id,
            apply_url=parsed["apply_url"],
            description=parsed["description_html"],
            method="ea.avature_careers.jobdetail.description_requirements_article",
            description_url=parsed["detail_url"],
            locations=locations,
            department=parsed["department"],
            employment_type=parsed["worker_type"],
            workplace_type=parsed["work_model"],
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            salary=parsed["salary"],
            qualifications=parsed["qualifications"],
            responsibilities=parsed["responsibilities"],
        )
        job.description_text = parsed["description_text"]
        job.employer = "Electronic Arts"
        job.record_identity = f"electronic-arts:{role_id}"
        return job

    def _unavailable_job(self, row, reason):
        raw = {
            "listing": row,
            "detail_fetch_failed": True,
            "detail_error": reason,
            "source_ui": {"portal_page": self.PORTAL_PAGE, "detail_url": row["href"]},
        }
        locations = [{"summary": row["listing_location"]}] if row.get("listing_location") else []
        job = self.job(
            raw,
            title=row["title"],
            url=row["href"],
            job_id=row["job_id"],
            requisition_id=row["job_id"],
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=locations,
            department=row.get("listing_department"),
            employment_type=row.get("listing_worker_type"),
        )
        job.description_unavailable = True
        job.warnings.append("detail_fetch_failed")
        job.record_identity = f"electronic-arts:{row['job_id']}"
        return job

    async def _detail(self, row):
        url = row["href"]
        try:
            response = await self.http.get(url)
            self._check_url(response.url, hosts={"jobs.ea.com"},
                            path_prefixes=("/en_US/careers/JobDetail/",))
            if detail_job_id(str(response.url)) != row["job_id"]:
                raise ValueError(
                    f"EA detail request redirected to a different Role ID: {response.url}")
            parsed = parse_detail_page(response.text, str(response.url), row)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"EA JobDetail {row['job_id']} ({url}): {reason}")
            return self._unavailable_job(row, reason)
        if parsed["attachment_links"]:
            self.result.errors.append(
                f"{row['job_id']}: EA published role attachment links are present "
                "but no full attachment text was captured")
        return self._make_job(row, parsed)

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        parts = urlsplit(entry_url)
        if (parts.scheme != "https"
                or parts.hostname not in {"www.ea.com", "ea.com"}
                or parts.path.rstrip("/") != "/careers"):
            raise ValueError("EA dedicated flow must start from its official https://www.ea.com/careers entry")
        if not self.page_guard():
            return self.result
        entry = await self.http.get(entry_url)
        self._check_url(entry.url, hosts={"www.ea.com", "ea.com"}, path_prefixes=("/careers",))
        if "jobs.ea.com" not in entry.text:
            raise ValueError("EA official entry no longer publishes a jobs.ea.com call-to-action")
        portal = await self.http.get("https://jobs.ea.com")
        self._check_url(portal.url, hosts={"jobs.ea.com"}, path_prefixes=("/en_US/careers",))
        if str(portal.url).rstrip("/") != self.PORTAL_PAGE:
            raise ValueError(f"EA portal no longer lands on the inspected listing: {portal.url}")

        listing_started = time.perf_counter()
        try:
            initial_pages, initial_rows, total = await self._listing_sweep(
                first_url=self.PORTAL_PAGE, label="initial")
        except Exception as exc:
            self.result.errors.append(f"EA initial company listing failed: {type(exc).__name__}: {exc}")
            self.result.expected_total = 0
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": str(entry.url),
                "portal_page_url": str(portal.url),
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            return self.result
        listing_seconds = time.perf_counter() - listing_started

        detail_started = time.perf_counter()
        stubs = []
        for row in initial_rows:
            stub = self.job(
                {"listing": row},
                title=row["title"], url=row["href"], job_id=row["job_id"],
                requisition_id=row["job_id"],
            )
            stub.record_identity = f"electronic-arts:{row['job_id']}"
            stubs.append(stub)
        # self.add is bypassed here: hydrate() replaces stubs with full jobs.
        hydrated = await self.hydrate(
            stubs, lambda stub: self._detail(next(
                row for row in initial_rows if row["job_id"] == stub.job_id)))
        for job in hydrated:
            self.add(job)
        detail_seconds = time.perf_counter() - detail_started

        recheck_started = time.perf_counter()
        final_snapshot_verified = True
        final_pages, final_rows, final_total = [], [], None
        try:
            final_pages, final_rows, final_total = await self._listing_sweep(
                first_url=listing_page_url(0), label="final")
        except Exception as exc:
            final_snapshot_verified = False
            self.result.errors.append(
                "EA final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}")
        recheck_seconds = time.perf_counter() - recheck_started
        initial_ids = [row["job_id"] for row in initial_rows]
        final_ids = [row["job_id"] for row in final_rows] if final_rows else []
        membership_stable = (final_snapshot_verified and final_total == total
                             and final_ids == initial_ids)

        self.result.expected_total = total
        if membership_stable and not self.result.errors:
            self.result.listing_complete = True
        else:
            self.result.listing_complete = False
            if final_snapshot_verified and not membership_stable:
                self.result.errors.append(
                    "ea_official_listing_membership_changed_between_initial_and_final_snapshots")
                for job in self.result.jobs:
                    if job.job_id not in set(final_ids):
                        job.warnings.append("membership_changed_during_collection")

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "portal_page_url": str(portal.url),
            "portal_listing_url": self.PORTAL_PAGE,
            "listing_page_url_template": listing_page_url(0),
            "membership_authority": (
                "Electronic Arts official https://www.ea.com/careers entry -> "
                "https://jobs.ea.com Avature listing /en_US/careers; every "
                "article.article--result Role ID across all jobOffset pages is "
                "membership. No facet, keyword or sort filter applied; only "
                "exact published JobDetail hrefs fetched."),
            "listing_filter_behavior": (
                "unfiltered: portal facets Country/Job Category/Worker Type/"
                "Studio/Department/Work Model and keyword search were not "
                "submitted; pagination links carry only jobRecordsPerPage/"
                "jobOffset"),
            "ui_pagination": (
                f"{PAGE_SIZE} cards per page; jobOffset steps of {PAGE_SIZE}; "
                "legend range/total reconciled per page"),
            "facet_names": [
                "Country (8171)", "Job Category (4536)", "Worker Type (4537)",
                "Studio/Department (4538)", "Work Model (4539)",
            ],
            "search_total": total,
            "initial_listing_pages": [
                {"offset": page["offset"], "url": page["url"],
                 "legend_range": page["legend_range"],
                 "expected_total": page["expected_total"], "ids": page["ids"]}
                for page in initial_pages
            ],
            "final_listing_pages": [
                {"offset": page["offset"], "url": page["url"],
                 "legend_range": page["legend_range"],
                 "expected_total": page["expected_total"], "ids": page["ids"]}
                for page in final_pages
            ],
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "final_snapshot_verified": final_snapshot_verified,
            "membership_stable": membership_stable,
            "detail_url_pattern": "/en_US/careers/JobDetail/<slug>/<Role ID> (slug ignored by server; Role ID authoritative)",
            "apply_url_pattern": "/en_US/careers/ApplicationMethods?jobId=<Role ID> (link retained; form not opened)",
            "description_source": (
                "each exact JobDetail page's Description & Requirements "
                "article content; General Information supplies locations, "
                "Role ID, Worker Type, Studio/Department, Work Model"),
            "dates": "no posted/updated date is exposed in the portal HTML detail; dates stay null",
            "salary_policy": (
                "PAY RANGES block retained verbatim when published in a full "
                "description; other compensation remains null"),
            "rss_feed_note": (
                "/Home/feed/ ignores jobOffset and diverges from the HTML "
                "listing UI; inspected and rejected as non-authoritative"),
            "job_attachment_links": {},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_listing_requests": len(initial_pages),
            "final_listing_requests": len(final_pages),
            "detail_requests": len(initial_rows),
        })
        # Attachment presence is checked per detail; record non-empty maps only.
        attachment_map = {}
        for job in self.result.jobs:
            links = job.raw.get("source_ui", {}).get("attachment_links", [])
            if links:
                attachment_map[job.job_id] = links
        self.result.metadata["job_attachment_links"] = attachment_map
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["final_recheck_phase_seconds"] = round(recheck_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
            "collection_wall_seconds"]
        return self.result
