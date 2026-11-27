"""D. E. Shaw India's own SSR careers listing and slug detail journey.

Membership authority is the ``regularJobs`` array server-rendered into the
official ``/careers`` page's ``__NEXT_DATA__`` JSON, restricted to rows whose
``data.activeOnJobsListing`` is true and which appear as ``/careers/<jobUrl>``
cards in the same HTML (the company's own visible list).  The ``internalJobs``
array (``activeOnJobsListing`` false) is internal-only and is never fetched.

Each visible job's full sections come from its own exact detail page's
``jobData`` (``/careers/<jobUrl>``): ``websiteDescription`` (intro),
``responsibilities`` (``What you’ll do day-to-day``) and
``peopleWeAreLookingForHtml`` (the rendered ``Who we’re looking for`` body,
authoritative over the plain ``peopleWeAreLookingFor`` fields, which can
diverge and are null for 36 roles while the HTML exists).  The detail page's
``datePosted`` equals the SSR request time (verified: it advanced with every
fetch), so it is never used as a posting date.  The apply portal URL is
retained from detail HTML but never fetched.  No ATS board is enumerated.
"""
import hashlib
import html
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
CARD_HREF = re.compile(r"^/careers/(?P<slug>[a-z0-9-]+)-(?P<id>\d+)$")
APPLY_URL = "https://www.apply.deshawindia.com/ApplicationPage1.html?entity=DESIS"
RESPONSIBILITIES_HEADING = "What you’ll do day-to-day"
QUALIFICATIONS_HEADING = "Who we’re looking for"
EMPLOYER = "D. E. Shaw India Private Limited"


def compact_body_hash(value):
    """NFC/whitespace-free hash of a description body (Atlan convention)."""
    value = unicodedata.normalize("NFC", text_html(value))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_description_html(website, responsibilities, qualifications_html):
    """Compose the page's three rendered sections into one HTML body.

    The page renders ``websiteDescription`` as the intro paragraph,
    ``responsibilities`` under ``What you’ll do day-to-day`` and
    ``peopleWeAreLookingForHtml`` verbatim under ``Who we’re looking for``.
    Plain-text sections are escaped; the qualifications fragment is kept
    verbatim.  Exploratory pipeline roles carry the intro only.
    """
    parts = []
    if website:
        parts.append(f"<p>{html.escape(website)}</p>")
    if responsibilities:
        parts.append(
            f"<h2>{RESPONSIBILITIES_HEADING}</h2><div>{html.escape(responsibilities)}</div>"
        )
    if qualifications_html:
        parts.append(f"<h2>{QUALIFICATIONS_HEADING}</h2>{qualifications_html}")
    return "".join(parts)


def _job_attachment_links(value):
    soup = BeautifulSoup(value or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _salary_from_description(value):
    for line in (value or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if re.search(r"\b(salary|pay range|CTC|LPA)\b", text, re.I) or "₹" in text:
            return {"source_text": text, "source_field": "deshaw.detail.jobData.jobDescription"}
    return None


def _parse_next_data(value, label):
    match = NEXT_DATA.search(value or "")
    if not match:
        raise ValueError(f"D.E. Shaw {label} page has no __NEXT_DATA__ JSON")
    try:
        payload = json.loads(match.group(1))
    except ValueError as exc:
        raise ValueError(f"D.E. Shaw {label} page __NEXT_DATA__ is not valid JSON") from exc
    if payload.get("page") != "/careers/[[...slug]]":
        raise ValueError(f"D.E. Shaw {label} page changed from its observed Next.js route")
    page_props = payload.get("props", {}).get("pageProps")
    if not isinstance(page_props, dict):
        raise ValueError(f"D.E. Shaw {label} page has no pageProps")
    return payload, page_props


def _visible_rows(page_props, label):
    regular = page_props.get("regularJobs")
    internal = page_props.get("internalJobs")
    if not isinstance(regular, list) or not isinstance(internal, list):
        raise ValueError(f"D.E. Shaw {label} page has no regularJobs/internalJobs arrays")
    if page_props.get("jobsFetchingError") is True:
        raise ValueError(f"D.E. Shaw {label} page reports jobsFetchingError")
    visible = []
    for row in regular:
        data = row.get("data") if isinstance(row, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"D.E. Shaw {label} listing contains a malformed job row")
        if data.get("activeOnJobsListing") is True:
            visible.append(row)
    for row in internal:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and data.get("activeOnJobsListing") is True:
            raise ValueError(
                f"D.E. Shaw {label} page lists an active job outside regularJobs"
            )
    ids = [row["data"].get("id") for row in visible]
    if any(not isinstance(identity, int) for identity in ids):
        raise ValueError(f"D.E. Shaw {label} listing contains a missing or invalid exact job ID")
    if len(ids) != len(set(ids)):
        raise ValueError(f"D.E. Shaw {label} listing repeats a company-published ID")
    for row in visible:
        data = row["data"]
        if not data.get("displayName"):
            raise ValueError(f"D.E. Shaw {label} listing contains an unidentified job")
        job_url = data.get("jobUrl")
        if not isinstance(job_url, str) or not job_url.endswith(f"-{data['id']}"):
            raise ValueError(f"D.E. Shaw {label} listing job {data.get('id')} has an unexpected jobUrl")
    return visible, internal


def _card_links(entry_html):
    soup = BeautifulSoup(entry_html, "lxml")
    found = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        match = CARD_HREF.fullmatch(href)
        if match:
            found.append((href, match.group("slug"), int(match.group("id"))))
    return found


def _apply_urls(entry_html):
    soup = BeautifulSoup(entry_html, "lxml")
    return sorted({anchor.get("href", "").strip() for anchor in soup.select("a[href]")
                   if "apply.deshawindia.com" in anchor.get("href", "")})


class Deshaw(Adapter):
    """Follow only the SSR listing and exact detail pages D.E. Shaw publishes."""

    VERIFICATION_REVISION = "deshaw.ssr-listing-detail-pipeline-exclusions.1"
    CAREERS_HOST = "www.deshawindia.com"

    @staticmethod
    def _assert_deshaw_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != Deshaw.CAREERS_HOST:
            raise ValueError(f"D.E. Shaw page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"D.E. Shaw page asset has an unexpected path: {url}")

    @staticmethod
    def _expected_locations(detail):
        locations = detail.get("jobMetadata", {}).get("jobLocations") or []
        return list(locations)

    def _make_job(self, listing_row, detail_row, *, detail_url, apply_url,
                  rendered=None, membership_changed=False):
        source_row = detail_row if detail_row is not None else listing_row["data"]
        identity = source_row.get("id")
        if not isinstance(identity, int):
            raise ValueError("D.E. Shaw row has no exact numeric job ID")
        listing_id = listing_row["data"].get("id")
        if listing_id != identity:
            raise ValueError(f"D.E. Shaw listing/detail ID mismatch: {listing_id} vs {identity}")
        if detail_row is not None and listing_row["data"].get("displayName") != detail_row.get("displayName"):
            raise ValueError(f"D.E. Shaw listing/detail title mismatch for {identity}")
        job_url = source_row.get("jobUrl")
        if not isinstance(job_url, str) or not job_url.endswith(f"-{identity}"):
            raise ValueError(f"D.E. Shaw job {identity} has an unexpected jobUrl")
        job_desc = source_row.get("jobDescription") or {}
        website = job_desc.get("websiteDescription")
        if not isinstance(website, str) or not website.strip():
            self.result.errors.append(f"{identity}: D.E. Shaw public websiteDescription is empty")
        responsibilities = job_desc.get("responsibilities")
        qualifications_html = job_desc.get("peopleWeAreLookingForHtml")
        if qualifications_html is not None and not isinstance(qualifications_html, str):
            raise ValueError(f"D.E. Shaw job {identity} has a non-text qualifications field")
        description_html = build_description_html(
            website or "", responsibilities, qualifications_html
        )
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            self.result.errors.append(
                f"{identity}: D.E. Shaw public detail description is implausibly short"
            )
        job_metadata = source_row.get("jobMetadata") or {}
        categories = [item.get("name") for item in (source_row.get("jobCategory") or [])
                      if isinstance(item, dict) and item.get("name")]
        department = (source_row.get("department") or {}).get("name") or None
        department = department.strip() if isinstance(department, str) else None
        if department == "":
            department = None
        team = " / ".join(categories) or None
        attachment_links = _job_attachment_links(description_html)
        salary = _salary_from_description(description_text)
        is_exploratory = bool(job_metadata.get("isExploratory"))
        raw = {
            "listing": listing_row,
            "detail": detail_row,
            "source_ui": {
                "native_job_url": detail_url,
                "card_href": listing_row.get("_card_href"),
                "rendered_apply_url": apply_url,
                "apply_portal_url": APPLY_URL,
                "listing_data_id": listing_id,
                "hire_type_id": (source_row.get("hireType") or {}).get("id"),
                "job_headers": source_row.get("jobHeaders"),
                "job_categories": categories,
                "is_exploratory": is_exploratory,
                "for_recent_graduates": job_metadata.get("forRecentGraduates"),
                "work_status": job_metadata.get("workStatus"),
                "writing_sample_required": job_metadata.get("writingSampleRequired"),
                "publication_required": job_metadata.get("publicationRequired"),
                "website_page_order": job_metadata.get("websitePageOrder"),
                "valid_to_date": source_row.get("validToDate"),
                "extended_video_url": source_row.get("extendedVideoUrl"),
                "attachment_links": attachment_links,
                "salary_from_description": salary,
                "rendered": rendered or {},
            },
        }
        job = self.job(
            raw,
            title=source_row.get("displayName"),
            url=detail_url,
            job_id=str(identity),
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="deshaw.official_careers.ssr_jobData_sections",
            description_url=detail_url,
            locations=self._expected_locations(source_row),
            department=department,
            team=team,
            employment_type=job_metadata.get("workStatus"),
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=salary,
            qualifications=qualifications_html,
            responsibilities=responsibilities,
            employer=EMPLOYER,
        )
        job.description_text = description_text
        job.record_identity = f"deshaw:{identity}"
        if is_exploratory:
            job.warnings.append("exploratory_all_positions_pipeline")
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        if attachment_links:
            job.warnings.append("job_description_attachment_links_present_unfetched")
        return job

    async def _listing_snapshot(self, entry_url, label):
        response = await self.http.get(entry_url)
        final = str(response.url)
        if final.rstrip("/") != entry_url.rstrip("/"):
            raise ValueError(f"D.E. Shaw {label} listing request redirected away from its official entry")
        payload, page_props = _parse_next_data(response.text, label)
        visible, internal = _visible_rows(page_props, label)
        cards = _card_links(response.text)
        expected = [(f"/careers/{row['data']['jobUrl'].lower()}", row["data"]["id"])
                    for row in visible]
        actual = [(href, identity) for href, _slug, identity in cards]
        if [path.lower() for path, _ in expected] != [path.lower() for path, _ in actual] or \
                [identity for _, identity in expected] != [identity for _, identity in actual]:
            raise ValueError(
                f"D.E. Shaw {label} rendered card links (href/ID/order) do not match its SSR listing"
            )
        apply_urls = _apply_urls(response.text)
        if apply_urls != [APPLY_URL]:
            raise ValueError(f"D.E. Shaw {label} listing apply portal URL changed from its observed value")
        if page_props.get("notMainUrl") is True:
            raise ValueError(f"D.E. Shaw {label} listing page unexpectedly sets notMainUrl")
        return {
            "response": response,
            "payload": payload,
            "page_props": page_props,
            "visible": visible,
            "internal": internal,
            "cards": cards,
            "apply_url": apply_urls[0],
        }

    def _parse_detail(self, detail_html, expected_id, detail_url):
        payload, page_props = _parse_next_data(detail_html, "detail")
        job_data = page_props.get("jobData")
        if not isinstance(job_data, dict):
            raise ValueError(f"D.E. Shaw detail {detail_url} has no jobData")
        if job_data.get("id") != expected_id:
            raise ValueError(
                f"D.E. Shaw detail {detail_url} jobData ID differs from its listed card"
            )
        if job_data.get("activeOnJobsListing") is not True:
            raise ValueError(f"D.E. Shaw detail {detail_url} is not an active public posting")
        soup = BeautifulSoup(detail_html, "lxml")
        heading = soup.find("h1")
        h1_text = heading.get_text(" ", strip=True) if heading else ""
        h1_text = " ".join(h1_text.split())
        title = " ".join(str(job_data.get("displayName") or "").split())
        if title and h1_text != title:
            raise ValueError(f"D.E. Shaw detail {detail_url} rendered H1 differs from jobData displayName")
        h2_texts = [" ".join(node.get_text(" ", strip=True).split())
                    for node in soup.find_all("h2") if node.get_text(" ", strip=True)]
        apply_urls = _apply_urls(detail_html)
        if apply_urls != [APPLY_URL]:
            raise ValueError(f"D.E. Shaw detail {detail_url} apply portal URL changed from its observed value")
        has_responsibilities = job_data.get("jobDescription", {}).get("responsibilities") is not None
        has_qualifications = job_data.get("jobDescription", {}).get("peopleWeAreLookingForHtml") is not None
        if has_responsibilities and RESPONSIBILITIES_HEADING not in h2_texts:
            raise ValueError(f"D.E. Shaw detail {detail_url} lacks its rendered responsibilities heading")
        if has_qualifications and QUALIFICATIONS_HEADING not in h2_texts:
            raise ValueError(f"D.E. Shaw detail {detail_url} lacks its rendered qualifications heading")
        rendered = {
            "rendered_h1": h1_text or None,
            "rendered_h2": h2_texts,
            "apply_url": apply_urls[0],
        }
        return payload, job_data, rendered

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname != self.CAREERS_HOST
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("D.E. Shaw dedicated flow must start from its official HTTPS /careers entry")
        initial = await self._listing_snapshot(entry_url, "initial")
        initial_props = initial["page_props"]
        initial_visible = initial["visible"]
        if not initial_visible:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "D.E. Shaw official listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result
        card_by_id = {identity: href for href, _slug, identity in initial["cards"]}
        first_identity = initial_visible[0]["data"]["id"]
        first_detail_url = urljoin(entry_url, card_by_id[first_identity])
        self._assert_deshaw_url(first_detail_url, path_prefix="/careers/")
        detail_started = time.perf_counter()
        representative_error = None
        representative_rendered = None
        representative_job_data = None
        try:
            rep_response = await self.http.get(first_detail_url)
            self._assert_deshaw_url(rep_response.url, path_prefix="/careers/")
            _payload, representative_job_data, representative_rendered = self._parse_detail(
                rep_response.text, first_identity, first_detail_url
            )
        except Exception as exc:
            representative_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(
                f"D.E. Shaw representative exact detail page {first_detail_url}: {representative_error}"
            )
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        final_visible = initial_visible
        final_props = initial_props
        final_cards = initial["cards"]
        try:
            final = await self._listing_snapshot(entry_url, "final")
            final_visible = final["visible"]
            final_props = final["page_props"]
            final_cards = final["cards"]
        except Exception as exc:
            final_snapshot_verified = False
            self.result.errors.append(
                "D.E. Shaw final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        card_by_id.update({identity: href for href, _slug, identity in final_cards})
        initial_by_id = {row["data"]["id"]: row for row in initial_visible}
        final_by_id = {row["data"]["id"]: row for row in final_visible}
        # Listing order is the page's presentation order; preserve it for the union.
        all_ids = [row["data"]["id"] for row in final_visible]
        all_ids += [identity for identity in initial_by_id if identity not in final_by_id]
        initial_ids = [row["data"]["id"] for row in initial_visible]
        final_ids = [row["data"]["id"] for row in final_visible]
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append(
                "deshaw_official_listing_membership_changed_between_initial_and_final_snapshots"
            )
        changed_rows = sorted(
            str(identity) for identity in initial_by_id.keys() & final_by_id.keys()
            if initial_by_id[identity] != final_by_id[identity]
        )

        listing_jobs = []
        for identity in all_ids:
            listing_row = dict(final_by_id.get(identity) or initial_by_id[identity])
            listing_row["_card_href"] = card_by_id.get(identity)
            job = self._make_job(
                listing_row, None,
                detail_url=urljoin(entry_url, card_by_id.get(identity)
                                   or f"/careers/{listing_row['data']['jobUrl'].lower()}"),
                apply_url=initial["apply_url"],
                membership_changed=changed_membership and identity not in final_by_id,
            )
            self.add(job)
            listing_jobs.append(job)

        async def hydrate_one(listing_job):
            listing_row = listing_job.raw["listing"]
            identity = listing_row["data"]["id"]
            detail_url = listing_job.url
            response = await self.http.get(detail_url)
            self._assert_deshaw_url(response.url, path_prefix="/careers/")
            _payload, job_data, rendered = self._parse_detail(
                response.text, identity, detail_url
            )
            return self._make_job(
                listing_row, job_data,
                detail_url=detail_url, apply_url=rendered["apply_url"],
                rendered=rendered,
                membership_changed=changed_membership and identity not in final_by_id,
            )

        hydrated = await self.hydrate(self.result.jobs, hydrate_one)
        self.result.jobs = list(hydrated)
        for job in self.result.jobs:
            if job.raw.get("listing") is not None:
                job.raw["listing"] = {k: v for k, v in job.raw["listing"].items() if k != "_card_href"}
                job.raw["source_ui"]["card_href"] = card_by_id.get(int(job.job_id))

        self.result.expected_total = len(final_visible)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        unlisted_ids = sorted({
            str(row["data"]["id"]) for row in initial["internal"] + final_props.get("internalJobs", [])
            if isinstance(row.get("data"), dict) and row["data"].get("id") is not None
        })
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(initial["response"].url),
            "membership_authority": (
                "D.E. Shaw /careers server-rendered regularJobs with activeOnJobsListing true, "
                "in page order, excluding internalJobs (activeOnJobsListing false); rendered "
                "/careers/<jobUrl> cards in the same HTML must match exactly"
            ),
            "listing_filter_behavior": (
                "unfiltered regularJobs; 'I am interested in' category and office selectors "
                "(regularFilters) are client-side over jobCategory/jobLocations; "
                "'Viewing 4 of N' with +/- expander slices the same array client-side"
            ),
            "ui_pagination": "No server pagination; the SSR page embeds all visible jobs; the Jobs component shows slice(0,4) plus slice(4) behind an expander",
            "regular_filters": initial_props.get("regularFilters"),
            "initial_regular_rows": len(initial_visible),
            "final_regular_rows": len(final_visible),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "internal_ids_excluded_by_company_ui": unlisted_ids,
            "internal_count_excluded": len(unlisted_ids),
            "source_rows_changed_between_snapshots": changed_rows,
            "representative_detail_page": first_detail_url,
            "representative_detail_h1_matches": representative_rendered is not None,
            "representative_detail_sections": (representative_rendered or {}).get("rendered_h2"),
            "representative_detail_error": representative_error,
            "representative_detail_page_refetched_in_hydration": True,
            "description_source": "Each exact detail page jobData: websiteDescription (intro) + responsibilities (What you'll do day-to-day) + peopleWeAreLookingForHtml (Who we're looking for, rendered DOM authority)",
            "qualifications_policy": (
                "peopleWeAreLookingForHtml is the rendered qualifications body; "
                "peopleWeAreLookingFor/peopleWeAreLookingForStr are retained verbatim in raw.detail but "
                "diverge for some roles (e.g. ID 6938: plain 2-3 years vs HTML 8-10 years) and are null "
                "while the HTML exists for 36 roles"
            ),
            "date_policy": (
                "Detail jobData.datePosted is the SSR request time (observed to advance with every fetch "
                "and to equal the fetch minute), never a posting date; date_posted/date_posted_raw/"
                "date_updated/valid_through remain null and verbatim datePosted is retained only in raw.detail"
            ),
            "salary_policy": "No structured compensation or salary field is published; monetary-looking "
                             "text such as AUM is not treated as salary; salary remains null unless a "
                             "salary/pay-range/CTC/LPA line is present",
            "apply_policy": "apply portal URL retained from listing/detail HTML; the portal is never fetched and no application is opened",
            "job_attachment_links": {
                job.job_id: job.raw["source_ui"]["attachment_links"]
                for job in self.result.jobs if job.raw.get("source_ui", {}).get("attachment_links")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
