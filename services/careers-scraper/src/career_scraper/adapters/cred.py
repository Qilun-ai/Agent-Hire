"""CRED's own /openings listing and native /openings/<id> detail journey.

Membership authority is CRED's official openings UI. Its shipped listings
chunk fetches the public board below and applies one client-side filter
(department `prefr` excluded, case-insensitive) before rendering. The native
detail page for an exact `/openings/<id>` renders that same board row's
`content.lists` sections only; the row's `content.description`, when present,
is not rendered by CRED and is retained only in the verbatim raw row. No Lever
board-wide enumeration, prefr scope, or application submission is performed.
"""
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter
from .public_ats import epoch
from ..models import text_html


UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
KONG_JOBS = "https://kong-web-prod.dreamplug.in/lever-jobs-api/v1/jobs"
KONG_HOST = "kong-web-prod.dreamplug.in"
KONG_PATH = "/lever-jobs-api/v1/jobs"
LISTING_PAGE = "https://careers.cred.club/careers/openings"
NATIVE_ORIGIN = "https://careers.cred.club"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
TEXT_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"CRED official listing job ID is not a UUID: {identity!r}")
    return f"{NATIVE_ORIGIN}/openings/{identity}"


def native_apply_post_endpoint(identity):
    if not UUID.fullmatch(str(identity)):
        raise ValueError(f"CRED official listing job ID is not a UUID: {identity!r}")
    return f"https://api.cred.club/v1/postings/{identity}/apply"


def _description_text(description_html):
    """Extract text like the rendered page: block breaks, inline spans joined.

    text_html() inserts separators between every BS4 string node, which added
    spaces before punctuation inside inline spans (e.g. "...engagement .").
    The browser renders those inline nodes contiguously, so use block-level
    breaks only.
    """
    soup = BeautifulSoup(description_html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    parts = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        block = node.name in TEXT_BLOCK_TAGS
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    for child in soup.children:
        walk(child)
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _description_html(row):
    """Build the body CRED's native /openings/<id> page renders.

    The detail component renders title plus content.lists headings/content. It
    does not render content.description; that field stays in raw.listing (the
    verbatim source row) but is deliberately excluded from the description.
    """
    parts = []
    content = row.get("content") or {}
    for section in content.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("text") or "").strip()
        body = section.get("content") or ""
        if heading:
            from html import escape

            parts.append(f"<h2>{escape(heading)}</h2>")
        if str(body).strip():
            parts.append(str(body))
    return "\n".join(parts)


class Cred(Adapter):
    """Follow only CRED's live openings requests and exact native detail routes."""

    VERIFICATION_REVISION = "cred.official-openings-board-and-native-detail.1"
    CAREERS_HOSTS = {"careers.cred.club", "www.careers.cred.club"}

    # ------------------------------------------------------------------
    # Observed request shapes
    # ------------------------------------------------------------------
    @staticmethod
    def _assert_careers_url(url, *, path_ok=None, path_prefix=None):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Cred.CAREERS_HOSTS:
            raise ValueError(f"CRED official page left the HTTPS careers site: {url}")
        if path_ok is not None and parsed.path not in path_ok:
            raise ValueError(f"CRED official page has an unexpected path: {url}")
        if path_prefix is not None and not parsed.path.startswith(path_prefix):
            raise ValueError(f"CRED official page has an unexpected path: {url}")
        return parsed

    @staticmethod
    def _next_data(html, label):
        match = NEXT_DATA.search(html or "")
        if not match:
            raise ValueError(f"CRED {label} page has no __NEXT_DATA__ payload")
        try:
            payload = json.loads(match.group(1))
        except ValueError as exc:
            raise ValueError(f"CRED {label} __NEXT_DATA__ is not valid JSON") from exc
        return payload

    @classmethod
    def _embedded_rows(cls, html, label, *, page_expected):
        payload = cls._next_data(html, label)
        if payload.get("page") != page_expected:
            raise ValueError(f"CRED {label} page changed from its observed Next route")
        page_props = payload.get("props", {}).get("pageProps", {})
        data = page_props.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"CRED {label} page has no data object")
        # List pages embed {"data": [rows]}; detail pages embed the row itself.
        if "data" in data and isinstance(data.get("data"), list):
            return payload, data["data"]
        return payload, data

    @staticmethod
    def _chunk_roots(html):
        soup = BeautifulSoup(html or "", "lxml")
        urls = []
        for node in soup.select('script[src*="_next/static/chunks/"]'):
            src = node.get("src") or ""
            if any(bad in src for bad in ("polyfills", "webpack", "framework", "main-", "pages/_app")):
                continue
            if "/pages/" in src:
                continue
            urls.append(src)
        # Openings page ships exactly the 4911 shared chunk and the 818 listings
        # chunk; keep source order so provenance is deterministic.
        return urls

    @staticmethod
    def _extract_listing_endpoint(script, label):
        urls = sorted({m.group(0) for m in re.finditer(
            r"https://kong-web-prod\.dreamplug\.in/lever-jobs-api/v1/jobs", script or ""
        )})
        if len(urls) != 1:
            raise ValueError(f"CRED {label} component does not expose exactly one embedded jobs fetch")
        url = urls[0]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != KONG_HOST
                or parsed.path != KONG_PATH or parsed.query or parsed.fragment):
            raise ValueError(f"CRED {label} component exposes an unexpected job source: {url}")
        if "getJobListing" not in (script or ""):
            raise ValueError(f"CRED {label} component changed its job-listing request shape")
        return url

    @staticmethod
    def _assert_listing_component(script, api_url):
        if api_url not in (script or ""):
            raise ValueError("CRED listings component no longer fetches its discovered jobs board")
        if "getJobListing" not in (script or ""):
            raise ValueError("CRED listings component no longer matches the inspected list/search/filter flow")

    @staticmethod
    def _assert_detail_component(script):
        # Detail chunk reads query.slug and posts the application to api.cred.club;
        # it must not reference a different jobs board.
        if "query.slug" not in (script or "") and "query" not in (script or ""):
            raise ValueError("CRED detail component changed its slug routing")
        if "https://api.cred.club" not in (script or ""):
            raise ValueError("CRED detail component no longer posts to its inspected apply endpoint host")

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError(f"CRED {snapshot_name} jobs response has no data array")
        rows = payload["data"]
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, str) or not UUID.fullmatch(identity) for identity in ids):
            raise ValueError(f"CRED {snapshot_name} jobs response contains a missing or invalid job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"CRED {snapshot_name} jobs response repeats a company-published ID")
        if any(not row.get("text") for row in rows):
            raise ValueError(f"CRED {snapshot_name} jobs response contains an unidentified job")
        for row in rows:
            categories = row.get("categories")
            if not isinstance(categories, dict) or not isinstance(categories.get("department"), str):
                raise ValueError(f"CRED {snapshot_name} jobs response row {row.get('id')} has no department")
            content = row.get("content")
            if not isinstance(content, dict) or not isinstance(content.get("lists"), list):
                raise ValueError(f"CRED {snapshot_name} jobs response row {row.get('id')} has malformed content")
        # This is the filter in CRED's shipped listings service module (42080/j):
        # keep every row except department "prefr" (case-insensitive). The same
        # filter builds the SSR __NEXT_DATA__ snapshot.
        visible = [row for row in rows
                   if (row.get("categories") or {}).get("department", "").lower() != "prefr"]
        excluded = [str(row.get("id")) for row in rows
                    if (row.get("categories") or {}).get("department", "").lower() == "prefr"]
        return visible, excluded

    def _make_job(self, row, *, api_url, listing_chunk_url, detail_page_url,
                  initial_row=None, membership_changed=False):
        identity = row["id"]
        expected_show = f"https://jobs.lever.co/cred/{identity}"
        expected_apply = expected_show + "/apply"
        if row.get("urls", {}).get("show") != expected_show:
            raise ValueError(f"CRED listed ID {identity} has an unexpected published show URL")
        apply_url = row["urls"].get("apply") or expected_apply
        if apply_url != expected_apply:
            raise ValueError(f"CRED listed ID {identity} has an unexpected published apply URL")
        description_html = _description_html(row)
        description_text = _description_text(description_html)
        if not description_text.strip():
            self.result.errors.append(f"{identity}: CRED public detail body is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: CRED public detail body is implausibly short")
        attachment_links = _job_attachment_links(description_html)
        categories = row.get("categories") or {}
        created_at = row.get("createdAt")
        if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
            date_posted = None
        else:
            date_posted = epoch(created_at)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": native_job_url(identity),
                "listed_show_url": expected_show,
                "listed_apply_url": apply_url,
                "native_apply_post_endpoint": native_apply_post_endpoint(identity),
                "listing_api_url": api_url,
                "listing_chunk_url": listing_chunk_url,
                "representative_detail_page_opened": detail_page_url,
                "attachment_links": attachment_links,
                "salary_from_description": None,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        location = categories.get("location")
        locations = [{"location": location}] if location else []
        job = self.job(
            raw,
            title=row["text"],
            url=native_job_url(identity),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="cred.official_openings_embed.content_lists",
            description_url=api_url,
            locations=locations,
            department=categories.get("department"),
            team=categories.get("team"),
            employment_type=categories.get("commitment"),
            workplace_type=row.get("workplaceType"),
            date_posted=date_posted,
            date_posted_raw=created_at,
            date_updated=None,
            salary=None,
        )
        job.description_text = description_text
        job.record_identity = f"cred:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    # ------------------------------------------------------------------
    # Flow
    # ------------------------------------------------------------------
    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        self._assert_careers_url(entry_url, path_ok={"", "/", "/careers"})
        entry = await self.http.get(entry_url)
        self._assert_careers_url(entry.url, path_ok={"", "/", "/careers"})
        if "CRED: Careers" not in entry.text:
            raise ValueError("CRED official entry page changed from its inspected shell")

        listing_started = time.perf_counter()
        listing_response = await self.http.get(LISTING_PAGE)
        self._assert_careers_url(listing_response.url, path_ok={"/careers/openings", "/openings"})
        # The alias host serves the same Next page; require the canonical page id.
        _, embedded_rows = self._embedded_rows(listing_response.text, "openings listing", page_expected="/careers/openings")
        if not isinstance(embedded_rows, list) or not embedded_rows:
            raise ValueError("CRED openings page embeds no job rows")
        embedded_ids = [r.get("id") for r in embedded_rows]
        if any(not isinstance(i, str) or not UUID.fullmatch(i) for i in embedded_ids):
            raise ValueError("CRED openings embedded snapshot contains an invalid job ID")
        chunk_urls = self._chunk_roots(listing_response.text)
        if len(chunk_urls) != 2:
            raise ValueError(f"CRED openings page ships an unexpected chunk set ({len(chunk_urls)})")
        listing_script = ""
        listing_chunk_url = None
        for candidate in chunk_urls:
            response = await self.http.get(candidate)
            if "getJobListing" in response.text or KONG_JOBS in response.text:
                listing_script = response.text
                listing_chunk_url = str(response.url)
                break
        if listing_chunk_url is None:
            raise ValueError("CRED openings listings chunk is missing from the shipped page")
        api_url = self._extract_listing_endpoint(listing_script, "openings listings")
        self._assert_listing_component(listing_script, api_url)
        listing_phase_seconds = time.perf_counter() - listing_started

        initial_payload = await self._listing_snapshot(api_url, "initial")
        initial_rows, initial_excluded = self._rows(initial_payload, "initial")
        initial_by_id = {row["id"]: row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "CRED official embedded listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result
        if [r["id"] for r in initial_rows] != embedded_ids:
            self.result.errors.append(
                "CRED live board membership differs from the openings page embedded SSG snapshot"
            )

        first_identity = initial_rows[0]["id"]
        exact_detail_page_url = native_job_url(first_identity)
        detail_started = time.perf_counter()
        detail_page_url = None
        detail_ui_error = None
        try:
            detail_page = await self.http.get(exact_detail_page_url)
            self._assert_careers_url(detail_page.url, path_prefix="/openings/")
            payload, detail_row = self._embedded_rows(
                detail_page.text, "representative detail", page_expected="/careers/openings/[slug]"
            )
            query_slug = (payload.get("query") or {}).get("slug")
            if query_slug != first_identity:
                raise ValueError("CRED exact native detail route resolved to a different slug")
            if not isinstance(detail_row, dict) or detail_row.get("id") != first_identity:
                raise ValueError("CRED native detail page does not embed its exact board row")
            if detail_row != initial_by_id[first_identity]:
                raise ValueError("CRED native detail embedded row differs from the live board row")
            detail_page_url = str(detail_page.url)
            # The detail page's own chunk ships with the page script; fetch it to
            # assert its slug/apply-host request shape. The filename rotates
            # with builds, so discover it from the detail HTML.
            soup = BeautifulSoup(detail_page.text, "lxml")
            detail_script_url = None
            for node in soup.select("script[src]"):
                src = node.get("src") or ""
                if "/pages/careers/openings/" in src:
                    detail_script_url = src
                    break
            if detail_script_url is None:
                raise ValueError("CRED native detail page ships no openings detail script")
            script_response = await self.http.get(detail_script_url)
            self._assert_detail_component(script_response.text)
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"CRED representative exact detail UI {exact_detail_page_url}: {detail_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_payload = await self._listing_snapshot(api_url, "final")
            final_rows, final_excluded = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            final_excluded = initial_excluded
            self.result.errors.append(
                "CRED final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["id"]: row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("cred_official_listing_membership_changed_between_initial_and_final_snapshots")
        if initial_excluded != final_excluded:
            self.result.errors.append("cred_prefr_excluded_set_changed_between_snapshots")

        # Board order is the UI order (filtered view preserves API order).
        all_ids = list(dict.fromkeys([row["id"] for row in final_rows] + [row["id"] for row in initial_rows]))
        changed_rows = [identity for identity in final_by_id.keys() & initial_by_id.keys()
                        if final_by_id[identity] != initial_by_id[identity]]
        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, api_url=api_url, listing_chunk_url=listing_chunk_url,
                detail_page_url=detail_page_url,
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_page_url": str(listing_response.url),
            "listing_chunk_url": listing_chunk_url,
            "listing_api_url": api_url,
            "membership_authority": "CRED openings listings chunk getJobListing board response, filtered exactly as shipped (department prefr excluded case-insensitively); SSG __NEXT_DATA__ snapshot corroborates the same order",
            "listing_filter_behavior": "rows where categories.department.lower() == 'prefr' are excluded; search/department/role controls are client-side over text, content lists, commitment and team and do not change unfiltered membership",
            "ui_pagination": "None; single GET returns the full board (live response has only a data array with no hasNext/next keys).",
            "department_filters": ["All", *sorted({row.get("categories", {}).get("department") for row in final_rows})],
            "initial_api_rows": len(initial_payload["data"]),
            "final_api_rows": len(final_payload["data"]),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [row["id"] for row in initial_rows],
            "final_visible_ids": [row["id"] for row in final_rows],
            "embedded_ssg_ids": embedded_ids,
            "prefr_excluded_ids": final_excluded,
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "exact_native_detail_page_opened": exact_detail_page_url,
            "representative_detail_page_final_url": detail_page_url,
            "representative_detail_embeds_exact_board_row": detail_ui_error is None,
            "representative_detail_ui_error": detail_ui_error,
            "description_source": "Each exact listed row's content.lists sections only (heading + content), matching what CRED's native detail component renders; content.description is retained verbatim in raw.listing but excluded from the description because the page omits it.",
            "description_text_method": "block-level HTML text extraction (inline spans joined, block tags separated) matching rendered innerText; no non-rendered board intro",
            "source_has_structured_compensation_field": False,
            "salary_policy": "No compensation field or salary text is exposed in the board bodies; salary stays null.",
            "job_attachment_links": {row["id"]: _job_attachment_links(_description_html(row))
                                      for row in final_rows if _job_attachment_links(_description_html(row))},
            "non_rendered_board_content_description_chars": {
                row["id"]: len(str((row.get("content") or {}).get("description") or ""))
                for row in final_rows
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["listing_discovery_seconds"] = round(listing_phase_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    async def _listing_snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"CRED {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"CRED {label} listing response is not JSON") from exc
        self._rows(payload, label)
        return payload
