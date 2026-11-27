"""CleverTap's own careers page and its Kula-hosted job board journey.

Observed live flow (2026-11-07, see ``companies/clevertap/research/journey.md``):

- Entry ``https://clevertap.com/careers/`` ships a Kula location grid script with an
  embedded public ``API_URL`` (``https://api.kula.ai/v1/job-boards/job-posts``) and a
  bearer ``API_TOKEN``. The script pages through that endpoint (``?page=N``, bounded
  by ``meta.total_pages``) and treats a post as open unless its ``status`` is
  closed/internal/draft/archived. Its "See open roles" CTA points to the
  company-owned ``https://clevertap.com/current-openings/`` page, which is an iframe
  of ``https://careers.kula.ai/clevertap``. The same board is linked four more
  times from the entry page (Explore Careers / See All Openings). A legacy
  embedded ``jobs_json`` here is broken (``Invalid Credentials``) and renders no
  job cards; it is evidence only, never a membership source.
- The Kula listing rows carry ``id`` (board posting id used in every URL),
  ``job_board_url`` (numeric detail URL), ``title``, ``posted_at``, ``job_id``
  (ATS requisition id), ``workplace``, ``employment_type``, ``department`` and
  ``offices``. Rows carry no description.
- ``GET https://careers.kula.ai/clevertap`` SSR-renders every open card in the
  board (department grouping, client-side department/location filters, no
  pagination) with "Apply Now" links to canonical ``/clevertap/<id>-<slug>``
  detail pages. Each listing row's ``job_board_url`` redirects to that canonical.
- Each detail page SSR-renders the full body plus a schema.org ``JobPosting``
  JSON-LD document whose ``description`` is the complete HTML body. The
  "Application form" is client-rendered; the board's Apply Now links target the
  detail page itself, so the canonical URL is both the job and apply URL.

The adapter fetches only the exact URLs the company journey exposes: the entry,
the company-owned current-openings page, the exact Kula listing pages named by
the entry script, the exact board, and the exact detail URL of every listed id.
It never enumerates an ATS board and never fetches the non-rendered
``job-posts/<id>`` detail API (that shape was observed once in research only;
see ``companies/clevertap/README.md``).
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

API_URL_RE = re.compile(r"var\s+API_URL\s*=\s*'([^']+)'")
API_TOKEN_RE = re.compile(r"var\s+API_TOKEN\s*=\s*'([^']+)'")
KULA_API_PATH = "/v1/job-boards/job-posts"
KULA_BOARD_URL = "https://careers.kula.ai/clevertap"
CURRENT_OPENINGS_URL = "https://clevertap.com/current-openings/"
BLOCKED_STATUSES = {"closed", "internal", "draft", "archived"}
CARD_ROUTE = re.compile(r"^/clevertap/(\d+)-[a-z0-9]+(?:-[a-z0-9]+)*$")
BOARD_ROUTE = re.compile(r"^/clevertap/(\d+)$")


def compact_body_hash(html):
    """Whitespace-free NFC hash used to compare structured and visible bodies."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _employment_type_label(value):
    return {"full_time": "Full-time", "contract": "Contract",
            "part_time": "Part-time", "internship": "Internship"}.get(value, value)


def _workplace_label(value):
    return {"office": "On-Site", "hybrid": "Hybrid", "remote": "Remote"}.get(value, value)


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class CleverTap(Adapter):
    """Follow only the Kula board and listing endpoint the official page publishes."""

    VERIFICATION_REVISION = "clevertap.kula-listing-and-detail-pages.exact-ids.1"
    COMPANY_HOSTS = {"clevertap.com", "www.clevertap.com"}
    BOARD_HOST = "careers.kula.ai"
    API_HOST = "api.kula.ai"

    @staticmethod
    def _assert_company_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in CleverTap.COMPANY_HOSTS:
            raise ValueError(f"CleverTap company page left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"CleverTap company page has an unexpected path: {url}")

    @staticmethod
    def _listing_endpoint(entry_html):
        url_match = API_URL_RE.search(entry_html)
        token_match = API_TOKEN_RE.search(entry_html)
        if not url_match or not token_match:
            raise ValueError("CleverTap official entry no longer embeds its Kula job-posts listing source")
        url = url_match.group(1)
        token = token_match.group(1)
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != CleverTap.API_HOST
                or parsed.path != KULA_API_PATH or parsed.query or parsed.fragment):
            raise ValueError(f"CleverTap entry embeds an unexpected job listing source: {url}")
        if not token or len(token) < 20:
            raise ValueError("CleverTap entry embeds no usable Kula board token")
        return url, token

    @staticmethod
    def _assert_listing_rows(rows, snapshot_name):
        ids = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"CleverTap {snapshot_name} listing row is not an object")
            identity = row.get("id")
            if not isinstance(identity, int) or identity <= 0:
                raise ValueError(f"CleverTap {snapshot_name} listing has a missing or invalid posting id")
            if not isinstance(row.get("title"), str) or not row["title"].strip():
                raise ValueError(f"CleverTap {snapshot_name} listing row {identity} has no title")
            board_url = row.get("job_board_url")
            parsed = urlsplit(str(board_url or ""))
            if (parsed.scheme != "https" or parsed.hostname != CleverTap.BOARD_HOST
                    or not BOARD_ROUTE.fullmatch(parsed.path) or int(BOARD_ROUTE.fullmatch(parsed.path).group(1)) != identity
                    or parsed.query or parsed.fragment):
                raise ValueError(
                    f"CleverTap {snapshot_name} listing row {identity} has an uninspected job board URL: {board_url!r}")
            ids.append(identity)
        if len(ids) != len(set(ids)):
            raise ValueError(f"CleverTap {snapshot_name} listing repeats a company-published posting id")
        return ids

    @staticmethod
    def _open_rows(rows):
        """Apply the entry page's isOpenJob filter exactly as shipped."""
        open_rows = []
        blocked = []
        for row in rows:
            status = str(row.get("status") or "").lower()
            if status in BLOCKED_STATUSES:
                blocked.append(row["id"])
            else:
                open_rows.append(row)
        return open_rows, blocked

    @staticmethod
    def _job_postings_from_detail(html):
        soup = BeautifulSoup(html, "lxml")
        postings = []
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                value = json.loads(node.string or "")
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("@type") == "JobPosting":
                postings.append(value)
        if len(postings) != 1:
            raise ValueError(
                f"CleverTap exact detail page does not carry exactly one JobPosting document ({len(postings)})")
        return soup, postings[0]

    def _make_job(self, row, *, detail, canonical_url, detail_component_note,
                  initial_row=None, membership_changed=False):
        identity = row["id"]
        requisition_id = row.get("job_id")
        title = detail.get("title")
        if title != row.get("title"):
            raise ValueError(
                f"CleverTap posting {identity} detail title {title!r} differs from listing title {row.get('title')!r}")
        identifier = str((detail.get("identifier") or {}).get("value", ""))
        if identifier != str(identity):
            raise ValueError(f"CleverTap posting {identity} detail identifier changed: {identifier!r}")
        employer = (detail.get("hiringOrganization") or {}).get("name")
        if employer != "CleverTap":
            raise ValueError(f"CleverTap posting {identity} has an unexpected hiring organization: {employer!r}")
        if detail.get("datePosted") is None or str(detail["datePosted"]).replace(".000Z", "Z") != row.get("posted_at"):
            raise ValueError(f"CleverTap posting {identity} detail datePosted differs from listing posted_at")
        description_html = detail.get("description") or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: CleverTap public detail description is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: CleverTap public detail description is implausibly short")
        locations = [{"location": office.get("location"), "name": office.get("name"),
                      "city": office.get("city"), "state": office.get("state"),
                      "country": office.get("country")}
                     for office in (row.get("offices") or [])]
        raw = {
            "listing": row,
            "detail": {
                "job_posting": detail,
                "canonical_url": canonical_url,
                "apply_form_client_side": True,
                "application_form_action": None,
                "detail_surface": detail_component_note,
            },
            "source_ui": {
                "listed_job_board_url": row.get("job_board_url"),
                "canonical_detail_url": canonical_url,
                "rendered_employment_type": _employment_type_label(row.get("employment_type")),
                "rendered_workplace_type": _workplace_label(row.get("workplace")),
                "attachment_links": _job_attachment_links(description_html),
                "listing_api_url": self.result.metadata.get("listing_api_url"),
                "board_url": KULA_BOARD_URL,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=canonical_url,
            job_id=str(identity),
            requisition_id=str(requisition_id) if requisition_id is not None else None,
            apply_url=canonical_url,
            description=description_html,
            method="clevertap.kula_detail_page.jobposting_jsonld",
            description_url=canonical_url,
            locations=locations,
            department=(row.get("department") or {}).get("name"),
            team=None,
            employment_type=row.get("employment_type"),
            workplace_type=row.get("workplace"),
            date_posted=row.get("posted_at"),
            date_posted_raw=row.get("posted_at"),
            date_updated=None,
            salary=None,
            employer=employer,
        )
        job.description_text = description_text
        job.record_identity = f"clevertap:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def _listing_snapshot(self, api_url, token, label):
        rows = []
        total = None
        total_pages = 1
        pages_fetched = []
        page = 1
        while page <= total_pages:
            page_url = f"{api_url}?page={page}"
            response = await self.http.get(page_url, headers={"Authorization": f"Bearer {token}"})
            if str(response.url) != page_url:
                raise ValueError(f"CleverTap {label} listing page {page} redirected away from its source")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError(f"CleverTap {label} listing page {page} is not JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError(f"CleverTap {label} listing page {page} has no data array")
            meta = payload.get("meta") or {}
            if total is None:
                total = meta.get("total")
                total_pages = meta.get("total_pages") or 1
            if not isinstance(total_pages, int) or total_pages < 1:
                raise ValueError(f"CleverTap {label} listing page {page} reports an invalid page count")
            page_rows = payload["data"]
            self._assert_listing_rows(page_rows, f"{label} page {page}")
            rows.extend(page_rows)
            pages_fetched.append({"page": page, "url": page_url, "rows": len(page_rows),
                                  "total": total, "total_pages": total_pages})
            page += 1
        ids = self._assert_listing_rows(rows, label)
        if total is not None and len(rows) != total:
            raise ValueError(
                f"CleverTap {label} listing reconciles to {len(rows)} rows, expected total {total}")
        return rows, ids, pages_fetched

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"clevertap.com", "www.clevertap.com"}
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("CleverTap dedicated flow must start from its official HTTPS /careers/ entry")
        entry = await self.http.get(entry_url)
        self._assert_company_url(entry.url, path_prefix="/careers")
        api_url, token = self._listing_endpoint(entry.text)
        self.result.metadata["listing_api_url"] = api_url
        entry_soup = BeautifulSoup(entry.text, "lxml")
        open_roles = [urljoin(str(entry.url), a["href"]) for a in entry_soup.select("a[href]")
                      if "current-openings" in a.get("href", "")]
        if open_roles != [CURRENT_OPENINGS_URL]:
            raise ValueError("CleverTap entry no longer links its company-owned current-openings page")
        legacy_broken = '"Invalid Credentials"' in entry.text and "jobs_json" in entry.text

        # Company-owned current-openings page: an iframe of the Kula board.
        openings_started = time.perf_counter()
        openings = await self.http.get(CURRENT_OPENINGS_URL)
        self._assert_company_url(openings.url, path_prefix="/current-openings")
        openings_soup = BeautifulSoup(openings.text, "lxml")
        iframes = [frame.get("src") for frame in openings_soup.select("iframe")]
        if KULA_BOARD_URL not in iframes:
            raise ValueError("CleverTap current-openings page no longer embeds the Kula board")
        openings_seconds = time.perf_counter() - openings_started

        initial_rows, _initial_ids, initial_pages = await self._listing_snapshot(api_url, token, "initial")
        initial_open, initial_blocked = self._open_rows(initial_rows)
        initial_by_id = {row["id"]: row for row in initial_open}
        if not initial_open:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "CleverTap official embedded listing returned zero open jobs; native empty state requires independent review")
            return self.result

        # The exact visible board for the same company-published membership.
        board_started = time.perf_counter()
        board = await self.http.get(KULA_BOARD_URL)
        if str(board.url) != KULA_BOARD_URL:
            raise ValueError("CleverTap Kula board page redirected away from its published URL")
        board_soup = BeautifulSoup(board.text, "lxml")
        board_by_id = {}
        for anchor in board_soup.select("a[href]"):
            match = CARD_ROUTE.fullmatch(urlsplit(anchor.get("href", "")).path)
            if not match:
                continue
            identity = int(match.group(1))
            if anchor.get_text(" ", strip=True) != "Apply Now":
                continue
            if identity not in initial_by_id:
                raise ValueError(f"CleverTap visible board exposes unlisted posting {identity}")
            if identity in board_by_id:
                raise ValueError(f"CleverTap visible board repeats posting {identity}")
            board_by_id[identity] = urljoin(KULA_BOARD_URL, anchor.get("href", ""))
        board_ids = sorted(board_by_id)
        if set(board_ids) != set(initial_by_id):
            raise ValueError(
                "CleverTap visible board membership differs from the official entry listing membership: "
                f"board-only {sorted(set(board_ids) - set(initial_by_id))} listing-only "
                f"{sorted(set(initial_by_id) - set(board_ids))}")
        board_seconds = time.perf_counter() - board_started
        listing_titles = {(row.get("title") or "").strip() for row in initial_open}
        board_titles = [node.get_text(" ", strip=True) for node in board_soup.select("p.chakra-text")]
        board_titles = [text for text in board_titles if text in listing_titles]

        final_snapshot_verified = True
        try:
            final_rows, final_ids, final_pages = await self._listing_snapshot(api_url, token, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_rows = initial_rows
            final_ids = _initial_ids
            final_pages = []
            self.result.errors.append(
                "CleverTap final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}")
        final_open, final_blocked = self._open_rows(final_rows)
        final_by_id = {row["id"]: row for row in final_open}
        changed_membership = final_snapshot_verified and set(final_by_id) != set(initial_by_id)
        if changed_membership:
            self.result.errors.append("clevertap_official_listing_membership_changed_between_initial_and_final_snapshots")
        changed_rows = [identity for identity in final_by_id.keys() & initial_by_id.keys()
                        if final_by_id[identity] != initial_by_id[identity]]

        detail_seconds = 0.0
        # Keep final rows for stable ids. On a moving listing, retain ids seen
        # in either snapshot and annotate stale initial-only rows so a partial
        # result does not silently discard usable evidence (Atlan pattern).
        union_ids = list(dict.fromkeys(
            [i for i in final_ids if i in final_by_id] + [i for i in _initial_ids if i in initial_by_id]))
        for identity in union_ids:
            row = final_by_id.get(identity) or initial_by_id.get(identity)
            assert row is not None
            detail_url = row["job_board_url"]
            started_detail = time.perf_counter()
            try:
                detail_page = await self.http.get(detail_url)
                canonical = str(detail_page.url)
                parsed = urlsplit(canonical)
                match = CARD_ROUTE.fullmatch(parsed.path)
                if (parsed.scheme != "https" or parsed.hostname != self.BOARD_HOST or not match
                        or int(match.group(1)) != identity):
                    raise ValueError(
                        f"CleverTap posting {identity} detail URL redirected to an uninspected route: {canonical}")
                _detail_soup, posting = self._job_postings_from_detail(detail_page.text)
            except Exception as exc:
                detail_seconds += time.perf_counter() - started_detail
                self.result.errors.append(
                    f"CleverTap exact detail page {detail_url} for posting {identity}: {type(exc).__name__}: {exc}")
                posting = None
                canonical = detail_url
            else:
                detail_seconds += time.perf_counter() - started_detail
            if posting is None:
                # Keep the listing record, but its description stays unverified.
                raw = {"listing": row, "detail": {"job_posting": None,
                        "canonical_url": canonical, "detail_surface": None},
                       "source_ui": {"listed_job_board_url": row.get("job_board_url"),
                                     "canonical_detail_url": canonical,
                                     "rendered_employment_type": _employment_type_label(row.get("employment_type")),
                                     "rendered_workplace_type": _workplace_label(row.get("workplace")),
                                     "attachment_links": [],
                                     "listing_api_url": api_url, "board_url": KULA_BOARD_URL}}
                job = self.job(
                    raw, title=row.get("title") or "", url=canonical,
                    job_id=str(identity),
                    requisition_id=str(row["job_id"]) if row.get("job_id") is not None else None,
                    apply_url=canonical, description="", method=None, description_url=None,
                    locations=[{"location": o.get("location"), "name": o.get("name"),
                                "city": o.get("city"), "state": o.get("state"),
                                "country": o.get("country")} for o in (row.get("offices") or [])],
                    department=(row.get("department") or {}).get("name"), team=None,
                    employment_type=row.get("employment_type"), workplace_type=row.get("workplace"),
                    date_posted=row.get("posted_at"), date_posted_raw=row.get("posted_at"),
                    date_updated=None, salary=None, employer="CleverTap")
                job.record_identity = f"clevertap:{identity}"
                self.add(job)
                continue
            self.add(self._make_job(
                row, detail=posting, canonical_url=canonical,
                detail_component_note="Kula detail page JobPosting JSON-LD; application form client-rendered",
                initial_row=initial_by_id.get(identity),
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        self.result.expected_total = len(final_open)
        self.result.pages = initial_pages[-1]["total_pages"] if initial_pages else 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_api_url": api_url,
            "current_openings_url": CURRENT_OPENINGS_URL,
            "current_openings_iframe": KULA_BOARD_URL,
            "board_url": KULA_BOARD_URL,
            "legacy_jobs_json_broken_invalid_credentials": legacy_broken,
            "membership_authority": "CleverTap /careers/ entry Kula location-grid listing request (paginated job-posts), cross-checked against the SSR Kula board cards linked from the official entry and the company current-openings iframe",
            "listing_filter_behavior": "Entry script isOpenJob: exclude status closed/internal/draft/archived; current rows carry no status field so no row is excluded by the company filter",
            "ui_pagination": "Kula listing API paginates (page/total_pages, limit 20). Kula board renders all cards on one page with client-side All departments/All locations filters; location cards link to the board.",
            "initial_pages": initial_pages,
            "final_pages": final_pages,
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [row["id"] for row in initial_rows],
            "final_visible_ids": [row["id"] for row in final_rows],
            "initial_open_ids": sorted(initial_by_id),
            "final_open_ids": sorted(final_by_id),
            "board_ids": board_ids,
            "board_card_titles": board_titles,
            "blocked_ids_excluded_by_company_filter": sorted(set(initial_blocked) | set(final_blocked)),
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "description_source": "Each exact listed posting's Kula detail page JobPosting JSON-LD description (SSR full body); detail canonical URL is the board's Apply Now destination and the application-form surface",
            "source_has_structured_compensation_field": False,
            "salary_policy": "No structured compensation on the observed journey; salary stays null. The non-rendered Kula job-post detail API (research probe only) exposes recruiter-internal budget fields and is not collected.",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_pages": len(initial_pages) + len(final_pages),
        })
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(board_seconds, 6)
        self.result.timings["current_openings_phase_seconds"] = round(openings_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
