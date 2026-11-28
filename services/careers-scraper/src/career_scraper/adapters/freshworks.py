"""Freshworks's own careers journey: official entry -> hosted listing -> exact detail pages.

Freshworks's official https://www.freshworks.com/company/careers/ page
publishes its vacancies only through the hosted listing
https://careers.smartrecruiters.com/Freshworks ("Explore jobs" CTAs).
That listing is server-rendered per location group and lazy-loads the
remaining groups through its own `/Freshworks/api/groups?page=N` and
per-location `/Freshworks/api/more?type=location&value=...&page=N`
endpoints (see companies/freshworks/research/). Membership is taken only
from those published pages; the SmartRecruiters board-listing endpoint is
never enumerated. For each exact ID the listing publishes, the exact
detail page on jobs.smartrecruiters.com supplies the full description,
and the exact posting record supplies the apply link (the detail page's
apply control is a JS oneclick anchor with no static href) plus a
section cross-check.
"""
import hashlib
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ENTRY = "https://www.freshworks.com/company/careers/"
HOSTED = "https://careers.smartrecruiters.com/Freshworks"
JOBS_HOST = "jobs.smartrecruiters.com"
API_DETAIL = "https://api.smartrecruiters.com/v1/companies/Freshworks/postings/{}"
JOB_HREF = re.compile(r"^https://jobs\.smartrecruiters\.com/Freshworks/(\d+)(?:-.*)?$")
REF_TOKEN = re.compile(r"^(?:P(?:-?\d+)?|REF[A-Z0-9]+)(?:_\d+)?$", re.I)


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_title(value):
    """Compare published titles without trailing-space artifacts from the ATS."""
    return " ".join((value or "").split())


def _ref_from_aria(aria_label):
    """Listing cards use aria-label="<Title> - <REF>"; return the REF token if present."""
    if not aria_label or " - " not in aria_label:
        return None
    token = aria_label.rpartition(" - ")[2].strip()
    return token if token and " " not in token and REF_TOKEN.match(token) else None


def parse_listing_cards(fragment_html):
    """Parse one listing fragment (full page 0 or an /api/groups//api/more body).

    Returns a list of groups in page order:
    {title, qty, more, cards}, where each card is
    {job_id, url, title, aria_label, requisition_id, listing_employment_type}.
    Fragments from /api/more carry bare <li> cards without a group wrapper;
    those are returned as a single group with title/qty None.
    """
    soup = BeautifulSoup(fragment_html or "", "lxml")
    groups = []
    for section in soup.select("section.js-group[data-qty]"):
        heading = section.select_one("h3")
        more = section.select_one("a.js-more[data-value][data-type='location'][data-more]")
        groups.append({
            "title": heading.get_text(" ", strip=True) if heading else None,
            "qty": int(section["data-qty"]),
            "more": {
                "type": more.get("data-type"),
                "value": more.get("data-value"),
                "endpoint": more.get("data-more"),
            } if more else None,
            "cards": _parse_cards(section),
        })
    bare = _parse_cards(soup)
    if bare and not groups:
        groups.append({"title": None, "qty": None, "more": None, "cards": bare})
    return groups


def _parse_cards(scope):
    cards = []
    for anchor in scope.select("a.js-job-ad-link[href]"):
        match = JOB_HREF.match(anchor.get("href", "").strip())
        if not match:
            continue
        title_el = anchor.select_one("h4.job-title")
        emp_el = anchor.select_one("p.job-desc span")
        cards.append({
            "job_id": match.group(1),
            "url": anchor["href"].strip(),
            "title": title_el.get_text(" ", strip=True) if title_el else None,
            "aria_label": anchor.get("aria-label"),
            "requisition_id": _ref_from_aria(anchor.get("aria-label")),
            "listing_employment_type": (
                emp_el.get_text(" ", strip=True) if emp_el else None
            ),
        })
    return cards


def parse_detail_page(html, page_url):
    """Extract the retained fields and full section descriptions from one exact detail page."""
    soup = BeautifulSoup(html or "", "lxml")
    title_el = soup.select_one("h1.job-title")
    title = title_el.get_text(" ", strip=True) if title_el else None
    loc_el = soup.select_one("spl-job-location")
    location = loc_el.get("formattedaddress") if loc_el else None
    workplace_type = loc_el.get("workplacetype") if loc_el else None
    emp_el = soup.select_one("li[itemprop='employmentType']")
    employment_type = emp_el.get_text(" ", strip=True) if emp_el else None
    date_el = soup.select_one("meta[itemprop='datePosted']")
    date_posted = date_el.get("content") if date_el else None
    org_el = soup.select_one("[itemprop='hiringOrganization'] meta[itemprop='name']")
    employer = org_el.get("content") if org_el else None
    sections = []
    for section in soup.select(".job-sections section.job-section[id]"):
        heading = section.select_one("h2.title")
        body = section.select_one(".wysiwyg")
        if not body:
            continue
        sections.append({
            "id": section.get("id"),
            "title": heading.get_text(" ", strip=True) if heading else None,
            "html": body.decode_contents(),
        })
    if not title:
        raise ValueError(f"Freshworks detail page has no job title: {page_url}")
    if not sections:
        raise ValueError(f"Freshworks detail page has no description sections: {page_url}")
    description_html = "\n".join(
        f'<section id="{s["id"]}"><h2>{s["title"] or ""}</h2>{s["html"]}</section>'
        for s in sections
    )
    return {
        "title": title,
        "location": location,
        "workplace_type": workplace_type,
        "employment_type": employment_type,
        "date_posted": date_posted,
        "employer": employer,
        "sections": sections,
        "description_html": description_html,
    }


class Freshworks(Adapter):
    """Follow only Freshworks's published entry, hosted listing and exact detail pages."""

    VERIFICATION_REVISION = "freshworks.official-careers-hosted-listing-and-exact-details.1"
    MAX_LISTING_PAGES = 50

    async def _listing_pass(self, label):
        """One full membership pass over the hosted listing.

        Returns (cards_by_id, meta) where meta records every page fetched,
        the per-group count reconciliation, and any soft listing errors
        (duplicates / count mismatches) without aborting the pass.
        Structural breaks (redirects, changed endpoints) raise instead.
        """
        cards_by_id = {}
        pages_meta = []
        soft_errors = []
        qty_mismatches = []
        expected_qty = 0
        expected_groups = None

        def absorb(card, **extra):
            card = dict(card, **extra)
            if card["job_id"] in cards_by_id:
                soft_errors.append(
                    f"Freshworks {label} listing repeats job ID {card['job_id']}; "
                    "first published occurrence retained"
                )
                return False
            cards_by_id[card["job_id"]] = card
            return True

        # Page 0: the server-rendered hosted listing itself.
        page_url = HOSTED
        page_number = 0
        while True:
            response = await self.http.get(page_url)
            if str(response.url) != page_url:
                raise ValueError(
                    f"Freshworks {label} listing request redirected: {page_url} -> {response.url}"
                )
            fragment = response.text
            if page_number == 0:
                soup = BeautifulSoup(fragment, "lxml")
                container = soup.select_one(".js-openings-load")
                if container is None or container.get("data-more") != "/Freshworks/api/groups":
                    raise ValueError(
                        "Freshworks hosted listing no longer exposes its observed "
                        "/Freshworks/api/groups group endpoint"
                    )
                expected_groups = int(container.get("data-groups-pages") or 0)
                if expected_groups < 1:
                    raise ValueError("Freshworks hosted listing reports no group pages")
            groups = parse_listing_cards(fragment)
            page_qty = sum(g["qty"] for g in groups if g["qty"] is not None)
            page_ids = []
            for group in groups:
                if group["qty"] is not None:
                    expected_qty += group["qty"]
                collected = 0
                for card in group["cards"]:
                    if absorb(card, listing_group=group["title"],
                              listing_page=page_number, listing_pass=label):
                        collected += 1
                        page_ids.append(card["job_id"])
                # Per-location "Show more" remainder pages for truncated groups.
                more = group["more"]
                if more:
                    if more["endpoint"] != "/Freshworks/api/more":
                        raise ValueError(
                            "Freshworks listing group exposes an unexpected 'more' endpoint: "
                            f"{more['endpoint']}"
                        )
                    more_page = 1
                    while True:
                        more_url = (
                            f"{HOSTED}/api/more?type=location&value={more['value']}&page={more_page}"
                        )
                        more_response = await self.http.get(more_url)
                        more_groups = parse_listing_cards(more_response.text)
                        more_cards = [c for g in more_groups for c in g["cards"]]
                        if not more_cards:
                            break
                        for card in more_cards:
                            if absorb(card, listing_group=group["title"],
                                      listing_pass=label, listing_more_page=more_page):
                                collected += 1
                                page_ids.append(card["job_id"])
                        more_page += 1
                        if more_page > self.MAX_LISTING_PAGES:
                            raise ValueError(
                                f"Freshworks {label} location {group['title']!r} pagination did not terminate"
                            )
                    pages_meta.append({
                        "pass": label, "kind": "more",
                        "url": f"{HOSTED}/api/more?type=location&value={more['value']}&page=1..",
                        "group": group["title"], "group_qty": group["qty"],
                        "pages": more_page - 1,
                    })
                if group["qty"] is not None and collected != group["qty"]:
                    qty_mismatches.append({
                        "pass": label, "group": group["title"],
                        "group_qty": group["qty"], "collected": collected,
                    })
            pages_meta.append({
                "pass": label, "kind": "groups", "url": page_url,
                "groups": [g["title"] for g in groups],
                "group_qty_sum": page_qty, "job_ids": page_ids,
            })
            if not groups:
                break
            page_number += 1
            if page_number > self.MAX_LISTING_PAGES:
                raise ValueError(f"Freshworks {label} group pagination did not terminate")
            page_url = f"{HOSTED}/api/groups?page={page_number}"
        return cards_by_id, {
            "pages": pages_meta,
            "expected_qty": expected_qty,
            "expected_groups": expected_groups,
            "soft_errors": soft_errors,
            "qty_mismatches": qty_mismatches,
        }

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https"
                or entry_parts.hostname not in {"www.freshworks.com", "freshworks.com"}
                or entry_parts.path.rstrip("/") != "/company/careers"):
            raise ValueError("Freshworks dedicated flow must start from its official HTTPS careers entry")
        entry = await self.http.get(entry_url)
        if str(entry.url) != entry_url:
            raise ValueError(f"Freshworks official entry redirected: {entry_url} -> {entry.url}")
        entry_soup = BeautifulSoup(entry.text, "lxml")
        ctas = [a["href"] for a in entry_soup.select("a[href]")
                if a.get("href", "").strip().rstrip("/") == HOSTED.rstrip("/")]
        if not ctas:
            raise ValueError(
                "Freshworks official careers page no longer publishes its hosted "
                "careers.smartrecruiters.com/Freshworks listing link"
            )

        initial_cards, initial_meta = await self._listing_pass("initial")
        if not initial_cards:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Freshworks hosted listing returned zero jobs; an empty board "
                "requires independent review before certification"
            )
            return self.result

        # A second full membership pass, before any detail work, so a moving
        # listing is detected against the exact published pages (Atlan pattern).
        final_snapshot_verified = True
        final_cards, final_meta = initial_cards, initial_meta
        try:
            final_cards, final_meta = await self._listing_pass("final")
        except Exception as exc:
            final_snapshot_verified = False
            final_cards, final_meta = initial_cards, initial_meta
            self.result.errors.append(
                f"Freshworks final hosted-listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        for meta in (initial_meta, final_meta):
            self.result.errors.extend(meta["soft_errors"])
            for mismatch in meta["qty_mismatches"]:
                self.result.errors.append(
                    f"Freshworks {mismatch['pass']} group {mismatch['group']!r}: "
                    f"published count {mismatch['group_qty']} != collected {mismatch['collected']}"
                )
        initial_ids = set(initial_cards)
        final_ids = set(final_cards)
        changed = final_snapshot_verified and initial_ids != final_ids
        if changed:
            self.result.errors.append(
                "freshworks_hosted_listing_membership_changed_between_initial_and_final_snapshots"
            )
        final_qty = final_meta["expected_qty"]
        all_ids = list(dict.fromkeys(list(final_cards) + list(initial_cards)))
        for identity in all_ids:
            card = final_cards.get(identity) or initial_cards[identity]
            job = self.job(
                {"listing": card},
                title=card["title"] or "",
                url=card["url"],
                job_id=identity,
                requisition_id=card["requisition_id"],
                description="",
                method=None,
                description_url=None,
            )
            job.record_identity = f"freshworks:{identity}"
            if identity not in final_cards:
                job.warnings.append("membership_changed_during_collection")
            self.add(job)

        async def detail(job):
            job_id = job.job_id
            # jobs.smartrecruiters.com content-negotiates: the shared client's
            # JSON-first Accept header yields a JSON job-ad payload at this URL,
            # while a browser receives the HTML detail page inspected in research.
            page = await self.http.get(
                job.url, headers={"Accept": "text/html,application/xhtml+xml"}
            )
            if str(page.url) != job.url:
                raise ValueError(
                    f"Freshworks exact detail page redirected: {job.url} -> {page.url}"
                )
            if "text/html" not in (page.headers.get("content-type") or ""):
                raise ValueError(
                    "Freshworks exact detail page did not return HTML "
                    f"({page.headers.get('content-type')}) for {job.url}"
                )
            parsed = parse_detail_page(page.text, job.url)
            api = await self.http.json(API_DETAIL.format(job_id))
            if str(api.get("id")) != job_id:
                raise ValueError(f"Freshworks exact posting record ID mismatch for {job_id}")
            api_sections = ((api.get("jobAd") or {}).get("sections")) or {}
            # Empty ATS sections (observed: a zero-length companyDescription)
            # are not rendered on the detail page, so they are not a missing
            # description section. Only sections with actual text must appear.
            api_displayed = {
                key: value for key, value in api_sections.items()
                if isinstance(value, dict) and text_html(value.get("text", "")).strip()
            }
            page_section_titles = [s["title"] for s in parsed["sections"]]
            api_section_titles = [value.get("title") for value in api_displayed.values()]
            if page_section_titles != api_section_titles:
                self.result.errors.append(
                    f"{job_id}: detail-page section titles {page_section_titles} "
                    f"differ from exact posting record {api_section_titles}"
                )
            if (_clean_title(parsed["title"]) != _clean_title(api.get("name"))
                    or _clean_title(parsed["title"]) != _clean_title(job.title)):
                self.result.errors.append(
                    f"{job_id}: listing/detail/posting titles differ "
                    f"({job.title!r}, {parsed['title']!r}, {api.get('name')!r})"
                )
            if parsed["date_posted"] and api.get("releasedDate") \
                    and parsed["date_posted"] != api["releasedDate"]:
                self.result.errors.append(
                    f"{job_id}: detail datePosted {parsed['date_posted']} differs "
                    f"from posting releasedDate {api['releasedDate']}"
                )
            by_title = {s["title"]: s for s in parsed["sections"]}
            api_by_title = {value.get("title"): value for value in api_displayed.values()}
            for title, section in api_by_title.items():
                page_section = by_title.get(title)
                if page_section is None or text_html(page_section["html"]) != text_html(section.get("text", "")):
                    self.result.errors.append(
                        f"{job_id}: section {title!r} text differs between the "
                        "detail page and the exact posting record"
                    )
            if api.get("postingUrl") != job.url:
                self.result.errors.append(
                    f"{job_id}: exact posting record postingUrl {api.get('postingUrl')!r} "
                    f"differs from the listing-published detail URL {job.url!r}"
                )
            job.raw.update({
                "detail_page": {
                    "location": parsed["location"],
                    "workplace_type": parsed["workplace_type"],
                    "employment_type": parsed["employment_type"],
                    "date_posted": parsed["date_posted"],
                    "employer": parsed["employer"],
                    "section_titles": page_section_titles,
                },
                "api_detail": api,
            })
            job.title = parsed["title"]
            job.description_html = parsed["description_html"]
            job.description_method = "freshworks.official_careers.hosted_detail_page.job_sections"
            job.description_source_url = job.url
            job.locations = [{"location": parsed["location"]}] if parsed["location"] else []
            job.workplace_type = parsed["workplace_type"]
            job.employment_type = parsed["employment_type"]
            job.date_posted = parsed["date_posted"]
            job.date_posted_raw = parsed["date_posted"]
            job.date_updated = None
            job.employer = parsed["employer"]
            job.apply_url = api.get("applyUrl")
            job.requisition_id = api.get("refNumber") or job.requisition_id
            func = api.get("function") or {}
            job.department = func.get("label") if isinstance(func, dict) else None
            exp = api.get("experienceLevel") or {}
            job.experience = exp.get("label") if isinstance(exp, dict) else None
            lang = api.get("language") or {}
            job.language = lang.get("label") if isinstance(lang, dict) else None
            compensation = api.get("compensation")
            if (isinstance(compensation, dict)
                    and compensation.get("min") == 0 and compensation.get("max") == 0):
                # The board publishes {min: 0, max: 0} when no salary is disclosed.
                # Keep the exact record in raw.api_detail but do not export a 0 salary.
                job.salary = None
                job.raw["salary_behavior"] = (
                    "ATS compensation min=max=0 (undisclosed); exact record retained in api_detail"
                )
            else:
                job.salary = compensation
            responsibilities = by_title.get("Job Description")
            qualifications = by_title.get("Qualifications")
            if responsibilities:
                job.responsibilities = text_html(responsibilities["html"])
            if qualifications:
                job.qualifications = text_html(qualifications["html"])
            return job

        await self.hydrate(self.result.jobs, detail)
        self.result.expected_total = final_qty
        # result.pages counts listing page fetches (group + more pages, both passes) + entry.
        self.result.pages = len(initial_meta["pages"]) + len(final_meta["pages"]) + 1
        if len(final_cards) != final_qty:
            self.result.errors.append(
                f"Freshworks final listing qty sum {final_qty} does not reconcile "
                f"to {len(final_cards)} unique job IDs"
            )
            self.result.listing_complete = False
        elif changed or not final_snapshot_verified:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "entry_hosted_listing_ctas": len(ctas),
            "hosted_listing_url": HOSTED,
            "membership_authority": (
                "Freshworks official /company/careers/ page's published hosted listing "
                "careers.smartrecruiters.com/Freshworks; membership is the union of its "
                "location-group pages (/Freshworks/api/groups) and per-location "
                "'Show more' remainder pages (/Freshworks/api/more)"
            ),
            "listing_filter_behavior": "No search or facet is applied; every location group and every 'Show more' page is collected unfiltered.",
            "ui_pagination": "Six location groups per group page; 9 cards per group page with the remainder behind per-location 'Show more' pages. Terminal pages are HTTP 200 with empty bodies.",
            "initial_pass": initial_meta,
            "final_pass": final_meta,
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": sorted(initial_ids),
            "final_visible_ids": sorted(final_ids),
            "source_rows_changed_between_snapshots": sorted(initial_ids ^ final_ids),
            "qty_sum_final": final_qty,
            "description_source": "The exact jobs.smartrecruiters.com detail page's .job-sections (Company Description, Job Description, Qualifications, Additional Information); the exact-ID posting record supplies the apply link and is cross-checked section-by-section.",
            "apply_source": "Exact-ID posting record applyUrl (== postingUrl + ?oga=true); the detail page's apply control is a JS oneclick anchor with no static href.",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        return self.result
