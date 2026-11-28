"""Expedia Group's own Appcast job-search listing and native detail-page flow.

Membership comes only from the official https://careers.expediagroup.com/jobs/
journey: its server-rendered results list plus its own "Load More" endpoint
(/calc-results/) for the unfiltered view and for every Category facet value the
official search form exposes. The unfiltered view's pages repeat URLs and omit
some published postings (170 advertised slots resolve to 133 distinct URLs), so
the union across the official views — which reconciles exactly to the page's own
advertised total — is the membership authority. Details are Expedia Group's own
/job/<slug>/<location>/<Req-id>/ pages; their schema.org JobPosting block is the
full-description source. Workday apply links are recorded exactly and never
opened.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

ENTRY_HOST = "careers.expediagroup.com"
ORIGIN = "https://careers.expediagroup.com"
REQ_ID = re.compile(r"^R-\d+(?:-\d+)?$")
DUTIES_HEADINGS = {
    "in this role you will", "in this role , you will", "in this role, you will",
    "what you'll do", "what you’ll do", "what you will do", "what you'll do:",
    "what you’ll do:", "what you will do:",
}
SALARY_RE = re.compile(r"\$[\d,]+(?:\.\d+)?\s*(?:to|-|–|—)\s*\$[\d,]+(?:\.\d+)?")


def compact_body_hash(html: str) -> str:
    """NFC/whitespace-free body hash used by the company audits."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace(" ", " ")).strip()


def _calc_url(home_url, *, category=None, mypage, token):
    base = home_url.rstrip("/") + "/calc-results/?"
    parts = []
    if category:
        parts.append(f"filter%5Bcategory%5D={quote(category, safe='')}")
    parts.append(f"mypage={mypage}")
    parts.append(f"ctok={quote(token, safe='')}")
    return base + "&".join(parts)


def _extract_cards(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()
    # Each card has two anchors with the same href (title link + button).
    for anchor in soup.select("a.view-job-button"):
        href = (anchor.get("href") or "").strip()
        if not href.startswith("/job/") or href in seen:
            continue
        seen.add(href)
        title_el = anchor.select_one(".Results__list__title")
        loc_el = anchor.select_one(".Results__list__location")
        cat_el = anchor.select_one("p")
        items.append({
            "href": href,
            "title": title_el.get_text(" ", strip=True) if title_el else "",
            "location": loc_el.get_text(" ", strip=True) if loc_el else "",
            "category": cat_el.get_text(" ", strip=True) if cat_el else "",
        })
    return items


def _parse_listing(html: str, *, view: str) -> dict:
    total_match = re.search(r'id="totresultsspan">\s*(\d+)', html)
    pages_match = re.search(r'numpages\s*=\s*"(\d+)"', html)
    token_match = re.search(r'calcToken":"([^"]+)"', html)
    home_match = re.search(r'"homeUrl":\s*"([^"]+)"', html)
    categories = []
    select = BeautifulSoup(html, "lxml").select_one('select[name="filter[category]"]')
    if select:
        for option in select.select("option"):
            value = (option.get("value") or "").strip()
            if value and value not in categories:
                categories.append(value)
    if "appcastPluginConfig" not in html:
        raise ValueError("Expedia listing page no longer exposes its Appcast Load More config")
    if not total_match or not pages_match or not token_match:
        raise ValueError("Expedia listing page is missing its advertised total/page/token config")
    return {
        "view": view,
        "claimed": int(total_match.group(1)),
        "numpages": int(pages_match.group(1)),
        "token": token_match.group(1),
        "home_url": (home_match.group(1).replace("\\/", "/") if home_match else ORIGIN),
        "items": _extract_cards(html),
        "categories": categories,
    }


def _jobposting(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")

    def walk(node):
        if isinstance(node, dict):
            if node.get("@type") == "JobPosting":
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        for posting in walk(data):
            return posting
    return None


def _visible_meta(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    values = []
    for listing in soup.select("ul.Info__list"):
        rows = [p.get_text(" ", strip=True) for p in listing.select("p")]
        if rows and rows not in values:
            values.append(rows)
    if not values:
        return {}
    row = list(values[0])
    # Align from the tail: the last two items are always [date, "ID # ..."'];
    # a location-less posting omits the location item entirely (4 items).
    id_cell = row[-1] if row else ""
    id_raw = id_cell.replace("ID #", "").strip() if id_cell.startswith("ID #") else id_cell
    return {
        "location": row[-5] if len(row) >= 5 else "",
        "category": row[-4] if len(row) >= 4 else "",
        "employment_visible": row[-3] if len(row) >= 3 else "",
        "date_raw": row[-2] if len(row) >= 2 else "",
        "id_raw": id_raw,
    }


def _salary_from_text(description_text: str) -> dict | None:
    for line in (description_text or "").splitlines():
        if SALARY_RE.search(line):
            return {
                "source_text": line.strip(),
                "source_field": "description_text",
                "semantics": "Published pay range text from the full job description; preserve source text without conversion.",
            }
    return None


def _duties_from_html(description_html: str) -> str | None:
    soup = BeautifulSoup(description_html or "", "lxml")
    for heading in soup.select("b, u, h1, h2, h3, h4, h5"):
        key = re.sub(r"\s+", " ", heading.get_text(" ", strip=True).strip().lower())
        if key not in DUTIES_HEADINGS:
            continue
        parts = []
        node = heading
        while True:
            node = node.next_element
            if node is None:
                break
            if getattr(node, "name", None) in {"b", "u", "h1", "h2", "h3", "h4", "h5"}:
                break
            if getattr(node, "name", None) == "p":
                text = node.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def _attachment_links(description_html: str) -> list:
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _apply_url(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select("a"):
        if anchor.get_text(" ", strip=True) == "Apply Now" and anchor.get("href"):
            return anchor.get("href")
    return None


async def _sleep_backoff(attempt: int):
    await asyncio.sleep(2 ** attempt)


class Expedia(Adapter):
    """Follow only Expedia Group's official listing views and native details."""

    VERIFICATION_REVISION = "expedia.official-appcast-listing-union-and-native-detail.1"
    MAX_DETAIL_ATTEMPTS = 3

    def _assert_company_url(self, url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {ENTRY_HOST, "www." + ENTRY_HOST}:
            raise ValueError(f"Expedia company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Expedia company page asset has an unexpected path: {url}")

    async def _get(self, url, *, headers=None, attempts: int = 3):
        last = None
        for attempt in range(attempts):
            try:
                return await self.http.get(url, headers=headers or {})
            except FetchError as exc:
                last = exc
                if attempt < attempts - 1:
                    await _sleep_backoff(attempt)
        raise last

    async def _fetch_view(self, page_url: str, *, view: str, category=None,
                          headers=None) -> dict:
        """Fetch one official view's SSR page and all of its Load More pages."""
        response = await self._get(page_url, headers=headers)
        self._assert_company_url(response.url, path_prefix="/jobs/")
        first = _parse_listing(response.text, view=view)
        record = {
            "view": view,
            "filter_category": category,
            "page_url": str(response.url),
            "claimed": first["claimed"],
            "numpages": first["numpages"],
            "pages": [],
        }
        seen: set[str] = set()
        records_by_url: dict[str, dict] = {}
        slots = 0

        def absorb(items, page_url_: str) -> int:
            nonlocal slots
            new = 0
            for item in items:
                slots += 1
                if item["href"] not in seen:
                    seen.add(item["href"])
                    new += 1
                    records_by_url[item["href"]] = item
            record["pages"].append({"url": page_url_, "slots": len(items), "new_urls": new})
            return new

        absorb(first["items"], str(response.url))
        for mypage in range(1, first["numpages"]):
            calc = _calc_url(first["home_url"], category=category,
                             mypage=mypage, token=first["token"])
            calc_response = await self._get(calc, headers=headers)
            if str(calc_response.url) != calc:
                raise ValueError(f"Expedia calc-results request redirected: {calc} -> {calc_response.url}")
            absorb(_extract_cards(calc_response.text), calc)
        record["slots_fetched"] = slots
        record["unique_urls"] = len(records_by_url)
        return {"record": record, "items_by_url": records_by_url,
                "categories": first["categories"]}

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {ENTRY_HOST, "www." + ENTRY_HOST}
                or entry_parts.path.rstrip("/") != "/jobs"):
            raise ValueError("Expedia dedicated flow must start from its official HTTPS /jobs/ entry")
        referer = {"Referer": entry_url}

        listing_started = time.perf_counter()
        views = []
        union: dict[str, dict] = {}
        view_of: dict[str, list[str]] = {}

        unfiltered = await self._fetch_view(entry_url, view="unfiltered", headers=referer)
        views.append(unfiltered["record"])
        for href, item in unfiltered["items_by_url"].items():
            union[href] = item
            view_of.setdefault(href, []).append("unfiltered")
        categories = unfiltered["categories"]
        if not categories:
            self.result.errors.append("Expedia entry page exposes no Category facet values")

        for category in categories:
            if self.result.pages >= self.options.max_pages:
                self.result.errors.append("page_budget_exhausted_during_category_views")
                break
            self.result.pages += 1
            filtered_url = entry_url.rstrip("/") + "/?filter%5Bcategory%5D=" + quote(category, safe="")
            view = await self._fetch_view(filtered_url, view=f"category:{category}",
                                          category=category, headers=referer)
            views.append(view["record"])
            for href, item in view["items_by_url"].items():
                if href not in union:
                    union[href] = item
                view_of.setdefault(href, []).append(f"category:{category}")
        listing_seconds = time.perf_counter() - listing_started

        claimed_total = views[0]["claimed"] if views else 0
        self.result.expected_total = claimed_total
        if len(union) != claimed_total:
            self.result.errors.append(
                f"Expedia official-view union ({len(union)} distinct URLs) does not reconcile "
                f"to the advertised unfiltered total ({claimed_total})"
            )
            self.result.listing_complete = False

        # Build stubs first so listing truth survives a failed detail fetch,
        # then hydrate each with its exact native detail page.
        jobs = []
        for href, item in union.items():
            absolute = ORIGIN + href if href.startswith("/") else href
            req_match = re.search(r"/(R-\d+(?:-\d+)?)/$", href)
            stub_id = req_match.group(1) if req_match else None
            if not stub_id or not REQ_ID.fullmatch(stub_id):
                self.result.errors.append(f"Expedia listed URL has no R-requisition tail: {href}")
                continue
            job = self.job(
                {"listing": {**item, "url": absolute}, "listing_views": view_of.get(href, [])},
                title=item["title"],
                url=absolute,
                job_id=stub_id,
                description="",
                locations=[{"location": item["location"]}] if item["location"] else [],
                department=item["category"] or None,
            )
            self.add(job)
            jobs.append(job)

        detail_started = time.perf_counter()
        hydrated = await self.hydrate(jobs, self._detail)
        detail_seconds = time.perf_counter() - detail_started

        if not self.result.errors:
            self.result.listing_complete = (len(hydrated) == claimed_total)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "membership_authority": (
                "Expedia Group's official /jobs/ search journey: its unfiltered Load More pages "
                "plus, because those pages repeat/omit URLs (170 slots -> 133 distinct), every "
                "Category value exposed by the official search form, unioned by exact job URL"
            ),
            "advertised_unfiltered_total": claimed_total,
            "category_values_from_official_form": categories,
            "views": views,
            "distinct_urls": len(union),
            "distinct_requisitions": len({job.job_id for job in jobs}),
            "unfiltered_slots": views[0]["slots_fetched"] if views else 0,
            "unfiltered_unique_urls": views[0]["unique_urls"] if views else 0,
            "listing_phase_seconds": round(listing_seconds, 6),
            "detail_phase_seconds": round(detail_seconds, 6),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "workday_apply_links_recorded_not_opened": True,
            "description_source": "Each exact /job/ detail page's schema.org JobPosting 'description' HTML; rendered .Desc__copy text must match.",
        })
        return self.result

    async def _detail(self, job):
        """Enrich a listing stub from its exact native detail page (with backoff)."""
        response = None
        last = None
        for attempt in range(self.MAX_DETAIL_ATTEMPTS):
            try:
                response = await self.http.get(job.url)
                break
            except FetchError as exc:
                last = exc
                if attempt < self.MAX_DETAIL_ATTEMPTS - 1:
                    await _sleep_backoff(attempt)
        if response is None:
            raise last
        self._assert_company_url(response.url, path_prefix="/job/")
        if str(response.url) != job.url:
            raise ValueError(f"Expedia detail request redirected: {job.url} -> {response.url}")
        posting = _jobposting(response.text)
        if not posting:
            raise ValueError(f"Expedia detail page {job.url} has no schema.org JobPosting block")
        identifier = posting.get("identifier")
        if identifier != job.job_id:
            raise ValueError(
                f"Expedia detail {job.url} identifier {identifier!r} differs from listing tail {job.job_id!r}"
            )
        from html import unescape

        title = unescape(posting.get("title") or "")
        if _norm_text(title) != _norm_text(job.title):
            raise ValueError(
                f"Expedia detail {job.url} title {title!r} differs from listing title {job.title!r}"
            )
        attachments = _attachment_links(posting.get("description") or "")
        if attachments:
            raise ValueError(
                f"Expedia detail {job.url} publishes role attachment links that were not captured: {attachments}"
            )
        description_html = posting.get("description") or ""
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            raise ValueError(f"Expedia detail {job.url} full description is implausibly short")
        visible = _visible_meta(response.text)
        copy_el = BeautifulSoup(response.text, "lxml").select_one(".Desc__copy")
        copy_text = text_html(str(copy_el)) if copy_el else ""
        if _norm_text(copy_text) != _norm_text(description_text):
            raise ValueError(f"Expedia detail {job.url} rendered description differs from JobPosting description")
        if visible.get("id_raw") and visible["id_raw"] != job.job_id:
            raise ValueError(
                f"Expedia detail {job.url} visible ID {visible['id_raw']!r} differs from {job.job_id!r}"
            )
        date_posted = posting.get("datePosted") or None
        if visible.get("date_raw"):
            try:
                visible_iso = datetime.strptime(visible["date_raw"], "%m/%d/%Y").date().isoformat()
            except ValueError:
                visible_iso = None
            if visible_iso and date_posted and visible_iso != date_posted:
                raise ValueError(f"Expedia detail {job.url} visible date differs from JobPosting datePosted")

        address = (posting.get("jobLocation") or {}).get("address") or {}
        location_entry = {
            "location": visible.get("location") or job.raw["listing"]["location"] or None,
            "city": address.get("addressLocality") or None,
            "region": address.get("addressRegion") or None,
            "country": address.get("addressCountry") or None,
            "postal_code": address.get("postalCode") or None,
        }
        employment_types = posting.get("employmentType") or []
        if isinstance(employment_types, str):
            employment_types = [employment_types]
        apply_url = _apply_url(response.text)
        salary = _salary_from_text(description_text)
        duties = _duties_from_html(description_html)
        job.title = title
        job.description_html = description_html
        job.description_text = description_text
        job.description_method = "expedia.official_job_detail.schema_description"
        job.description_source_url = job.url
        job.locations = [location_entry]
        job.department = visible.get("category") or job.department or None
        job.employment_type = employment_types[0] if employment_types else None
        job.date_posted = date_posted
        job.date_posted_raw = visible.get("date_raw") or None
        job.apply_url = apply_url
        job.salary = salary
        job.responsibilities = duties
        qualifications = posting.get("qualifications")
        job.qualifications = qualifications if qualifications else None
        experience = posting.get("experienceRequirements") or {}
        months = experience.get("monthsOfExperience") if isinstance(experience, dict) else None
        job.experience = {"months": months} if months else None
        job.employer = ((posting.get("hiringOrganization") or {}).get("name")) or "Expedia Group"
        job.raw["detail"] = {
            "jobposting": posting,
            "visible_meta": visible,
            "apply_url": apply_url,
            "attachment_links": attachments,
            "salary_from_description": salary,
            "duties_heading_extraction": duties,
            "rendered_copy_matches_jobposting": True,
        }
        return job
