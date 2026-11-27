"""DigitalOcean's own careers page and exact Greenhouse posting journey.

Corrected dedicated flow (2026-11-07), replacing the legacy browser-render
flow. See companies/digitalocean/research/journey-research.md for the full
delta. The legacy adapter remains described there; this file implements the
reviewed journey below and keeps no legacy behavior.

Observed journey (all URLs are company-published or company-embedded):

- Entry ``https://www.digitalocean.com/careers`` is a Next.js shell with an
  ``#open-roles`` section and a ``Loading roles...`` placeholder. Membership
  is client-side, so the adapter proves the shell and then reads only the
  shipped scripts below.
- Entry ``<script src>`` list contains exactly one
  ``app/careers/page-*.js`` chunk. That chunk imports the CurrentOpenings
  hook ``a(82909)`` and view ``a(50011)`` and renders ``id="open-roles"``.
- The entry script list also contains exactly one ``/5916-*.js`` chunk. That
  chunk (module ``58387``) defines the board literal ``"digitalocean98"`` and
  three exact templates:
  ``kT`` departments listing, ``kB`` single-job detail, ``D2`` Greenhouse
  widget script. Its hook does ``fetch(n.kT)`` and regroups rows by the
  ``Career Page Grouping`` metadata (``0x2b4db30``); its view links every
  location card to ``/careers/position/apply?gh_jid=<id>``.
- Membership is the unfiltered ``kT`` response:
  ``GET https://api.greenhouse.io/v1/boards/digitalocean98/embed/departments``.
  Search and location controls are client-side over title/locations. There is
  no pagination.
- Details are the exact ``kB`` row for each listed ID:
  ``GET https://api.greenhouse.io/v1/boards/digitalocean98/jobs/<id>``.
  The representative ``/careers/position/apply/?gh_jid=<id>`` page reads
  ``gh_jid`` and does ``(n.kB)(gh_jid)`` for its hero, then loads ``n.D2``
  (Greenhouse Iframe). The collector fetches that exact ``kB`` row and the
  representative apply page/chunk only; it never fetches the widget script
  and never submits an application.

The board slug is pinned to the inspected ``digitalocean98``. Any drift in
entry shell, chunk imports, board literal, or template hosts/paths raises
``ValueError`` before the board is requested (fail-closed, Atlan pattern).
"""

from __future__ import annotations

from html import unescape
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


EXPECTED_BOARD = "digitalocean98"
DEPARTMENTS_PATH = "/v1/boards/digitalocean98/embed/departments"
DEPARTMENTS_URL = f"https://api.greenhouse.io{DEPARTMENTS_PATH}"
JOB_URL_PREFIX = "https://api.greenhouse.io/v1/boards/digitalocean98/jobs/"
EMBED_JS_URL = "https://boards.greenhouse.io/embed/job_board/js?for=digitalocean98"
OFFICIAL_HOST = "www.digitalocean.com"
OFFICIAL_HOSTS = {"www.digitalocean.com", "digitalocean.com"}
APPLY_PATH = "/careers/position/apply/"
APPLY_URL_RE = re.compile(
    r"^https://www\.digitalocean\.com/careers/position/apply/\?gh_jid=(\d+)$"
)
JOB_ID_RE = re.compile(r"^\d+$")
ARRANGEMENT_METADATA_ID = 35320806  # 0x21af3e6, Career Page - Remote/Local
GROUPING_METADATA_ID = 45407024  # 0x2b4db30, Career Page Grouping
PAGE_CHUNK_MARK = "/app/careers/page-"
OPENINGS_CHUNK_MARK = "/5916-"
APPLY_CHUNK_MARK = "/app/careers/position/apply/page-"
ATTACHMENT_RE = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def apply_url_for(identity):
    """Company-published job/apply URL for one exact listed ID."""
    identity = str(identity)
    if not JOB_ID_RE.fullmatch(identity):
        raise ValueError(f"DigitalOcean listed job ID is not numeric: {identity!r}")
    return f"https://www.digitalocean.com/careers/position/apply/?gh_jid={identity}"


def job_detail_url_for(identity):
    """Exact kB single-job URL for one listed ID."""
    identity = str(identity)
    if not JOB_ID_RE.fullmatch(identity):
        raise ValueError(f"DigitalOcean listed job ID is not numeric: {identity!r}")
    return JOB_URL_PREFIX + identity


def decode_content(value):
    """Unescape the Greenhouse escaped-HTML wrapper once (if present).

    ``content`` arrives as ``&lt;div class=&quot;...&gt;...``. Decoding is
    applied only when the value looks like an escaped block wrapper, so
    literal technical examples in already-normal HTML are not corrupted.
    """
    text = value or ""
    for _ in range(2):
        if not (
            re.match(r"^\s*&(?:amp;)*lt;(?:div|p|section|article)(?:\s|&(?:amp;)*gt;)", text, re.I)
            and re.search(r"&(?:amp;)*lt;/(?:div|p|section|article)&(?:amp;)*gt;\s*$", text, re.I)
        ):
            break
        text = unescape(text)
    return text


def _metadata_value(metadata, identity):
    if not isinstance(metadata, list):
        return None
    for item in metadata:
        if isinstance(item, dict) and item.get("id") == identity:
            return item.get("value")
    return None


def career_page_grouping(metadata):
    """UI grouping key as shipped (module 82909): metadata value or DigitalOcean."""
    value = _metadata_value(metadata, GROUPING_METADATA_ID)
    return value if isinstance(value, str) and value.strip() else "DigitalOcean"


def career_page_arrangement(metadata):
    """UI arrangement as shipped (module 58387 ``_$``): value or Remote."""
    value = _metadata_value(metadata, ARRANGEMENT_METADATA_ID)
    return value if isinstance(value, str) and value.strip() else "Remote"


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if ATTACHMENT_RE.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def parse_departments_payload(payload, label):
    """Validate the exact kT envelope and return rows in API order.

    Returns ``(rows, ordered_ids)`` where ``rows`` is a list of
    ``{"native_department": str, "row": dict}`` in envelope order. Raises
    ``ValueError`` on any schema drift so a changed board is never mistaken
    for zero jobs.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
        raise ValueError(f"DigitalOcean {label} departments response has no departments array")
    rows = []
    seen = set()
    for department in payload["departments"]:
        if not isinstance(department, dict) or not isinstance(department.get("name"), str):
            raise ValueError(f"DigitalOcean {label} departments response has a malformed department")
        name = department["name"]
        jobs = department.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError(f"DigitalOcean {label} department {name!r} has no jobs array")
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError(f"DigitalOcean {label} listing contains a non-object job")
            identity = job.get("id")
            if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
                raise ValueError(f"DigitalOcean {label} listing job has a missing/invalid ID: {identity!r}")
            key = str(identity)
            if key in seen:
                raise ValueError(f"DigitalOcean {label} listing repeats job ID {key}")
            seen.add(key)
            title = job.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"DigitalOcean {label} listing job {key} has no title")
            location = job.get("location")
            if not isinstance(location, dict) or not isinstance(location.get("name"), str) or not location["name"].strip():
                raise ValueError(f"DigitalOcean {label} listing job {key} has no location name")
            expected_apply = apply_url_for(key)
            if job.get("absolute_url") != expected_apply:
                raise ValueError(
                    f"DigitalOcean {label} listing job {key} has an unexpected absolute_url: "
                    f"{job.get('absolute_url')!r} != {expected_apply!r}"
                )
            if job.get("company_name") != "DigitalOcean":
                raise ValueError(f"DigitalOcean {label} listing job {key} has an unexpected company_name")
            if not isinstance(job.get("metadata"), list):
                raise ValueError(f"DigitalOcean {label} listing job {key} has no metadata array")
            if _metadata_value(job["metadata"], ARRANGEMENT_METADATA_ID) is None and ARRANGEMENT_METADATA_ID not in {
                m.get("id") for m in job["metadata"] if isinstance(m, dict)
            }:
                raise ValueError(f"DigitalOcean {label} listing job {key} lacks Career Page - Remote/Local metadata")
            if _metadata_value(job["metadata"], GROUPING_METADATA_ID) is None and GROUPING_METADATA_ID not in {
                m.get("id") for m in job["metadata"] if isinstance(m, dict)
            }:
                raise ValueError(f"DigitalOcean {label} listing job {key} lacks Career Page Grouping metadata")
            # Dates/requisition must be present as source fields (deadline may be null).
            if not isinstance(job.get("first_published"), str) or not job["first_published"].strip():
                raise ValueError(f"DigitalOcean {label} listing job {key} has no first_published")
            if not isinstance(job.get("updated_at"), str) or not job["updated_at"].strip():
                raise ValueError(f"DigitalOcean {label} listing job {key} has no updated_at")
            rows.append({"native_department": name, "row": job})
    if not rows:
        raise ValueError(f"DigitalOcean {label} departments listing returned zero jobs; native empty state requires independent review")
    return rows, [str(entry["row"]["id"]) for entry in rows]


def ui_order(rows):
    """Deterministic UI-group order for stored per-ID records.

    Mirrors the shipped hook: group by ``Career Page Grouping`` metadata
    (sorted names), then trimmed title, then location name. Stored records
    remain per-ID; this only defines listing presentation order.
    """
    def key(entry):
        row = entry["row"]
        return (
            career_page_grouping(row.get("metadata")),
            (row.get("title") or "").strip().lower(),
            ((row.get("location") or {}).get("name") or "").lower(),
            str(row.get("id")),
        )

    return sorted(rows, key=key)


def parse_detail_payload(payload, *, identity, listing_row, label):
    """Validate one exact kB detail row against its listing row.

    Raises ``ValueError`` on ID/title/location/URL/company/schema drift so
    hydrate records a ``detail_fetch_failed`` partial rather than certifying
    a mismatched body.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"DigitalOcean {label} detail for {identity} is not an object")
    if str(payload.get("id")) != str(identity):
        raise ValueError(f"DigitalOcean {label} detail for {identity} returned another ID: {payload.get('id')!r}")
    if payload.get("title") != listing_row.get("title"):
        raise ValueError(f"DigitalOcean {label} detail {identity} title differs from its listing title")
    detail_location = payload.get("location")
    listing_location = listing_row.get("location")
    if not isinstance(detail_location, dict) or detail_location.get("name") != (listing_location or {}).get("name"):
        raise ValueError(f"DigitalOcean {label} detail {identity} location differs from its listing location")
    expected_apply = apply_url_for(identity)
    if payload.get("absolute_url") != expected_apply:
        raise ValueError(f"DigitalOcean {label} detail {identity} has an unexpected absolute_url")
    if payload.get("company_name") != "DigitalOcean" or listing_row.get("company_name") != "DigitalOcean":
        raise ValueError(f"DigitalOcean {label} detail {identity} has an unexpected company identity")
    # Shared source metadata must agree with the listing snapshot.
    for field in ("requisition_id", "internal_job_id", "first_published", "updated_at", "language"):
        if payload.get(field) != listing_row.get(field):
            raise ValueError(
                f"DigitalOcean {label} detail {identity} field {field!r} differs from its listing: "
                f"{payload.get(field)!r} != {listing_row.get(field)!r}"
            )
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError(f"DigitalOcean {label} detail {identity} has no content field")
    if not isinstance(payload.get("metadata"), list) or not isinstance(payload.get("departments"), list):
        raise ValueError(f"DigitalOcean {label} detail {identity} lacks metadata/departments")
    if not isinstance(payload.get("offices"), list):
        raise ValueError(f"DigitalOcean {label} detail {identity} lacks offices")
    return payload


class DigitalOcean(Adapter):
    """Follow DigitalOcean's shipped CurrentOpenings + exact apply journey."""

    VERIFICATION_REVISION = "digitalocean.current-openings-departments-and-exact-apply.2"
    DESCRIPTION_METHOD = "digitalocean.official_careers_embed.single_job_content"

    # -- Static discovery (Atlan component pattern, Next.js edition) ---------

    @staticmethod
    def _assert_official_url(url, *, path_prefix=None, path_ok=None):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
            raise ValueError(f"DigitalOcean official page left the HTTPS site: {url}")
        path = parsed.path.rstrip("/") or "/"
        if path_ok is not None and path not in path_ok and str(url).rstrip("/") not in path_ok and parsed.path not in path_ok:
            # Accept the exact entry path without trailing-slash games.
            if parsed.path not in path_ok:
                raise ValueError(f"DigitalOcean official page has an unexpected path: {url}")
        if path_prefix is not None and not parsed.path.startswith(path_prefix):
            raise ValueError(f"DigitalOcean official page has an unexpected path: {url}")
        return parsed

    @staticmethod
    def _script_urls(html, entry_url):
        soup = BeautifulSoup(html or "", "lxml")
        urls = []
        for node in soup.select("script[src]"):
            src = (node.get("src") or "").strip()
            if not src:
                continue
            absolute = urljoin(str(entry_url), src)
            urls.append(absolute)
        return urls

    @staticmethod
    def _chunk_url(html, entry_url, marker, label):
        candidates = [u for u in DigitalOcean._script_urls(html, entry_url) if marker in urlsplit(u).path]
        # Preserve entry order; the careers page chunk is unique by path.
        if len(candidates) != 1:
            raise ValueError(
                f"DigitalOcean official entry does not ship exactly one {label} chunk "
                f"({marker}): found {len(candidates)}"
            )
        url = candidates[0]
        parsed = urlsplit(url)
        # Chunks live under the build base path on the official host.
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS or "/_next/static/chunks/" not in parsed.path:
            raise ValueError(f"DigitalOcean official entry published an unexpected {label} chunk URL: {url}")
        return url

    @staticmethod
    def _assert_page_chunk(chunk):
        required = ("a(82909)", "a(50011)", '"open-roles"', "Open roles")
        if any(token not in chunk for token in required):
            raise ValueError("DigitalOcean careers page chunk no longer imports its inspected CurrentOpenings hook/view")

    @staticmethod
    def _parse_current_openings(chunk):
        """Extract and validate the 5916 board definition.

        Returns ``{"board", "departments_url", "job_url_template", "embed_js_url"}``.
        The template strings are rebuilt from the validated board literal so a
        template drift cannot smuggle another board/host.
        """
        if chunk.count('"digitalocean98"') != 1:
            raise ValueError("DigitalOcean CurrentOpenings chunk board literal changed or is ambiguous")
        for token in ("0x21af3e6", "0x2b4db30", "CurrentOpeningsStyles", "/embed/departments",
                      "boards.greenhouse.io/embed/job_board/js?for=", "api.greenhouse.io/v1/boards/"):
            if token not in chunk:
                raise ValueError(f"DigitalOcean CurrentOpenings chunk no longer matches its inspected source: {token!r} missing")
        if "fetch(" not in chunk or ".kT" not in chunk:
            raise ValueError("DigitalOcean CurrentOpenings hook no longer fetches its inspected departments listing")
        if "encodeURIComponent(e)" not in chunk and "encodeURIComponent" not in chunk:
            raise ValueError("DigitalOcean CurrentOpenings chunk no longer builds its inspected single-job URL")
        return {
            "board": EXPECTED_BOARD,
            "departments_url": DEPARTMENTS_URL,
            "job_url_template": "https://api.greenhouse.io/v1/boards/digitalocean98/jobs/${encodeURIComponent(e)}",
            "embed_js_url": EMBED_JS_URL,
        }

    @staticmethod
    def _assert_apply_chunk(chunk):
        if chunk.count('"digitalocean98"') != 1:
            raise ValueError("DigitalOcean apply chunk board literal changed or is ambiguous")
        for token in ('get("gh_jid")', ".kB", ".D2", "Grnhse", "Iframe", "load()", "0x21af3e6"):
            if token not in chunk:
                raise ValueError(f"DigitalOcean apply chunk no longer matches its inspected apply flow: {token!r} missing")
        for token in ("/embed/departments", "api.greenhouse.io/v1/boards/"):
            if token not in chunk:
                raise ValueError("DigitalOcean apply chunk no longer ships its inspected board definitions")

    # -- Listing snapshots ----------------------------------------------------

    async def _listing_snapshot(self, departments_url, label):
        response = await self.http.get(departments_url)
        if str(response.url) != departments_url:
            raise ValueError(f"DigitalOcean {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"DigitalOcean {label} listing response is not JSON") from exc
        entries, ordered_ids = parse_departments_payload(payload, label)
        return payload, entries, ordered_ids

    # -- Job construction / hydration -----------------------------------------

    def _make_job(self, entry, *, listing_api_url, detail_url, detail_row=None,
                  initial_entry=None, membership_changed=False):
        row = entry["row"]
        identity = str(row["id"])
        native_department = entry.get("native_department")
        if detail_row is None:
            # Listing-only fallback (hydrate failed). Retain the listing row;
            # validation will mark the missing description unverified.
            detail_row = row
        if detail_row is not entry["row"] and detail_row.get("content") is not None:
            parse_detail_payload(detail_row, identity=identity, listing_row=row, label="detail")
            content = detail_row.get("content") or ""
        else:
            content = detail_row.get("content") or ""
        description_html = decode_content(content)
        description_text = text_html(description_html)
        # Listing-only placeholders (detail_row is the listing row) have no
        # content until _detail hydrates them; only flag empty/short bodies
        # for hydrated rows so pre-hydration placeholders never poison errors.
        is_hydrated = detail_row is not row
        if is_hydrated:
            if not description_html.strip():
                self.result.errors.append(f"{identity}: DigitalOcean public detail content is empty")
            elif len(description_text.strip()) < 100:
                self.result.errors.append(f"{identity}: DigitalOcean public detail description is implausibly short")
        grouped = career_page_grouping(row.get("metadata"))
        arrangement = career_page_arrangement(row.get("metadata"))
        raw_arrangement = _metadata_value(row.get("metadata"), ARRANGEMENT_METADATA_ID)
        attachment_links = _job_attachment_links(description_html)
        pay = detail_row.get("pay_input_ranges")
        salary = pay if isinstance(pay, list) and pay else None
        locations = ([detail_row["location"]] if detail_row.get("location") else []) + (detail_row.get("offices") or [])
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": apply_url_for(identity),
                "rendered_apply_url": apply_url_for(identity),
                "listing_api_url": listing_api_url,
                "detail_api_url": detail_url,
                "native_department": native_department,
                "career_page_grouping": grouped,
                "career_page_arrangement": arrangement,
                "career_page_arrangement_raw": raw_arrangement,
                "attachment_links": attachment_links,
                "salary_from_description": None,
            },
        }
        if detail_row is not row:
            raw["detail"] = detail_row
        if initial_entry is not None and initial_entry["row"] != row:
            raw["initial_listing"] = initial_entry["row"]
        job = self.job(
            raw,
            title=detail_row.get("title") or row.get("title") or "",
            url=apply_url_for(identity),
            job_id=identity,
            requisition_id=detail_row.get("requisition_id"),
            apply_url=apply_url_for(identity),
            description=description_html,
            method=self.DESCRIPTION_METHOD if detail_row is not row or content else None,
            description_url=detail_url,
            locations=locations,
            department=detail_row.get("departments"),
            employment_type=None,
            workplace_type=None,
            date_posted=detail_row.get("first_published"),
            date_posted_raw=detail_row.get("first_published"),
            date_updated=detail_row.get("updated_at"),
            valid_through=detail_row.get("application_deadline"),
            salary=salary,
            language=detail_row.get("language"),
            employer=detail_row.get("company_name"),
        )
        job.description_text = description_text
        job.record_identity = f"digitalocean:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        detail_url = job_detail_url_for(identity)
        # Keep the exact observed request shape: plain GET, no query/body.
        response = await self.http.get(detail_url)
        if str(response.url) != detail_url:
            raise ValueError(f"DigitalOcean detail {detail_url} redirected to {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"DigitalOcean detail {identity} response is not JSON") from exc
        listing_row = job.raw.get("listing")
        if not isinstance(listing_row, dict):
            raise ValueError(f"DigitalOcean detail {identity} has no retained listing row")
        parse_detail_payload(payload, identity=identity, listing_row=listing_row, label="detail")
        description_html = decode_content(payload.get("content") or "")
        # Hydrated empty/short bodies are explicit errors (verify re-checks
        # every stored body), never silent partials.
        if not description_html.strip():
            self.result.errors.append(f"{identity}: DigitalOcean public detail content is empty")
        elif len(text_html(description_html).strip()) < 100:
            self.result.errors.append(f"{identity}: DigitalOcean public detail description is implausibly short")
        job.raw["detail"] = payload
        job.title = payload.get("title") or job.title
        job.description_html = description_html
        job.description_text = text_html(description_html)
        job.description_method = self.DESCRIPTION_METHOD
        job.description_source_url = detail_url
        job.requisition_id = payload.get("requisition_id")
        job.locations = ([payload["location"]] if payload.get("location") else []) + (payload.get("offices") or [])
        job.department = payload.get("departments")
        job.date_posted = payload.get("first_published")
        job.date_posted_raw = payload.get("first_published")
        job.date_updated = payload.get("updated_at")
        job.valid_through = payload.get("application_deadline")
        pay = payload.get("pay_input_ranges")
        job.salary = pay if isinstance(pay, list) and pay else None
        job.language = payload.get("language")
        job.employer = payload.get("company_name")
        job.raw["source_ui"]["detail_api_url"] = detail_url
        job.raw["source_ui"]["attachment_links"] = _job_attachment_links(description_html)
        return job

    # -- Main flow -------------------------------------------------------------

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        self._assert_official_url(entry_url, path_ok={"https://www.digitalocean.com/careers",
                                                      "https://www.digitalocean.com/careers/"})
        entry = await self.http.get(entry_url)
        self._assert_official_url(str(entry.url), path_ok={"https://www.digitalocean.com/careers",
                                                           "https://www.digitalocean.com/careers/"})
        entry_html = entry.text
        if "Open roles" not in entry_html or "Loading roles..." not in entry_html:
            raise ValueError("DigitalOcean official entry page changed from its inspected Open roles shell")
        if "Careers | DigitalOcean" not in entry_html:
            raise ValueError("DigitalOcean official entry page changed from its inspected careers title")
        page_chunk_url = self._chunk_url(entry_html, str(entry.url), PAGE_CHUNK_MARK, "careers page")
        openings_chunk_url = self._chunk_url(entry_html, str(entry.url), OPENINGS_CHUNK_MARK, "CurrentOpenings")
        page_chunk = await self.http.get(page_chunk_url)
        if str(page_chunk.url) != page_chunk_url:
            raise ValueError("DigitalOcean careers page chunk redirected away from its published URL")
        self._assert_page_chunk(page_chunk.text)
        openings_chunk = await self.http.get(openings_chunk_url)
        if str(openings_chunk.url) != openings_chunk_url:
            raise ValueError("DigitalOcean CurrentOpenings chunk redirected away from its published URL")
        board_info = self._parse_current_openings(openings_chunk.text)
        departments_url = board_info["departments_url"]

        initial_payload, initial_entries, initial_api_ids = await self._listing_snapshot(
            departments_url, "initial")
        initial_by_id = {str(entry["row"]["id"]): entry for entry in initial_entries}
        initial_ui_ids = [str(entry["row"]["id"]) for entry in ui_order(initial_entries)]

        # Representative exact apply page + its chunk (Atlan detail-UI phase).
        first_identity = initial_ui_ids[0]
        exact_apply_url = apply_url_for(first_identity)
        detail_started = time.perf_counter()
        apply_chunk_url = None
        apply_ui_error = None
        try:
            apply_page = await self.http.get(exact_apply_url)
            # The company-published card href uses .../apply/?gh_jid=; the
            # server canonicalizes to .../apply?gh_jid= (trailing-slash
            # redirect). Allow that exact canonicalization, but require the
            # same host/path/job ID.
            final_apply_url = str(apply_page.url)
            self._assert_official_url(final_apply_url, path_prefix="/careers/position/apply")
            if final_apply_url not in {exact_apply_url, exact_apply_url.replace("/?", "?")}:
                raise ValueError("DigitalOcean exact apply route redirected to a different URL")
            if first_identity not in apply_page.text:
                raise ValueError("DigitalOcean representative apply page does not reference its listed job ID")
            apply_chunk_url = self._chunk_url(apply_page.text, str(apply_page.url), APPLY_CHUNK_MARK, "apply")
            apply_chunk = await self.http.get(apply_chunk_url)
            if str(apply_chunk.url) != apply_chunk_url:
                raise ValueError("DigitalOcean apply chunk redirected away from its published URL")
            self._assert_apply_chunk(apply_chunk.text)
            # Apply chunk must define the same board/endpoints as the listing chunk.
            if board_info["board"] != EXPECTED_BOARD:
                raise ValueError("DigitalOcean apply chunk board differs from its listing board")
        except Exception as exc:
            apply_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"DigitalOcean representative apply UI {exact_apply_url}: {apply_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        # Hydrate every listed ID via its exact kB row.
        initial_jobs = [
            self._make_job(entry, listing_api_url=departments_url,
                           detail_url=job_detail_url_for(str(entry["row"]["id"])))
            for entry in initial_entries
        ]
        if self.options.max_jobs and len(initial_jobs) > self.options.max_jobs:
            initial_jobs = initial_jobs[: self.options.max_jobs]
            self.result.warnings.append("sample_limit")
        detail_started2 = time.perf_counter()
        if initial_jobs:
            await self.hydrate(initial_jobs, self._detail)
        detail_seconds = time.perf_counter() - detail_started2
        hydrated_by_id = {str(job.job_id): job for job in initial_jobs}

        final_snapshot_verified = True
        try:
            _final_payload, final_entries, final_api_ids = await self._listing_snapshot(
                departments_url, "final")
        except Exception as exc:
            final_snapshot_verified = False
            _final_payload, final_entries, final_api_ids = initial_payload, initial_entries, initial_api_ids
            self.result.errors.append(
                "DigitalOcean final company-published listing snapshot failed; "
                f"initial usable rows retained: {type(exc).__name__}: {exc}"
            )
        final_by_id = {str(entry["row"]["id"]): entry for entry in final_entries}
        final_ui_ids = [str(entry["row"]["id"]) for entry in ui_order(final_entries)]
        changed_membership = final_snapshot_verified and set(final_api_ids) != set(initial_api_ids)
        if changed_membership:
            self.result.errors.append(
                "digitalocean_official_listing_membership_changed_between_initial_and_final_snapshots")
        changed_rows = [identity for identity in set(final_by_id) & set(initial_by_id)
                        if final_by_id[identity]["row"] != initial_by_id[identity]["row"]]
        if changed_rows:
            self.result.errors.append("digitalocean_official_listing_fields_changed_between_snapshots")

        # Keep final rows for stable IDs; retain initial-only rows with a warning.
        all_ids = list(dict.fromkeys(final_ui_ids + [i for i in initial_ui_ids if i not in set(final_ui_ids)]))
        if self.options.max_jobs:
            all_ids = all_ids[: self.options.max_jobs]
        mapping_started = time.perf_counter()
        ordered_jobs = []
        for identity in all_ids:
            final_entry = final_by_id.get(identity)
            initial_entry = initial_by_id.get(identity)
            if final_entry is not None and initial_entry is not None:
                if final_entry["row"] != initial_entry["row"]:
                    # Listing drifted during hydration; refresh the listing half
                    # while keeping the hydrated exact-ID body provenance.
                    hydrated = hydrated_by_id.get(identity)
                    if hydrated is not None:
                        hydrated.raw["listing"] = final_entry["row"]
                        hydrated.raw["initial_listing"] = initial_entry["row"]
                        hydrated.raw["source_ui"]["native_department"] = final_entry.get("native_department")
                        ordered_jobs.append(hydrated)
                        continue
                else:
                    hydrated = hydrated_by_id.get(identity)
                    if hydrated is not None:
                        ordered_jobs.append(hydrated)
                        continue
            if final_entry is not None and identity not in hydrated_by_id:
                # New in final but not hydrated (membership grew). Fetch its exact
                # kB row now so the snapshot retains the usable record.
                job = self._make_job(final_entry, listing_api_url=departments_url,
                                     detail_url=job_detail_url_for(identity))
                try:
                    job = await self._detail(job)
                except Exception as exc:
                    job.warnings.append("detail_fetch_failed")
                    self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
                self.result.validate_job(job)
                self.seen.add(job.key)
                ordered_jobs.append(job)
                continue
            if final_entry is None and initial_entry is not None:
                hydrated = hydrated_by_id.get(identity)
                if hydrated is not None:
                    hydrated.warnings.append("membership_changed_during_collection")
                    ordered_jobs.append(hydrated)
        self.result.jobs = ordered_jobs
        self.result.dirty_jobs = list(ordered_jobs)
        # Revalidate in deterministic UI order.
        for job in self.result.jobs:
            self.result.validate_job(job)
        detail_mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(final_api_ids)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors and len(ordered_jobs) == len(final_api_ids)
        group_counts = {}
        for group_entry in final_entries:
            group = career_page_grouping(group_entry["row"].get("metadata"))
            group_counts[group] = group_counts.get(group, 0) + 1
        grouping_values = sorted(group_counts)
        distinct_titles = len({(career_page_grouping(group_entry["row"].get("metadata")),
                                 (group_entry["row"].get("title") or "").strip()) for group_entry in final_entries})
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "careers_page_chunk_url": page_chunk_url,
            "openings_chunk_url": openings_chunk_url,
            "apply_chunk_url": apply_chunk_url,
            "board": board_info["board"],
            "listing_api_url": departments_url,
            "job_url_template": board_info["job_url_template"],
            "embed_js_url": board_info["embed_js_url"],
            "membership_authority": "DigitalOcean CurrentOpenings hook fetch(n.kT) in its shipped 5916 chunk; unfiltered departments response",
            "listing_filter_behavior": "unfiltered departments rows are regrouped by Career Page Grouping metadata (0x2b4db30) and merged by trimmed title in the browser; query/location controls are client-side over title/locations",
            "ui_pagination": "No job pagination; one departments GET returns every department/job; the browser renders all groupings as collapsible sections",
            "department_filters": ["All", *grouping_values],
            "career_page_grouping_values": grouping_values,
            "career_page_grouping_id_counts": group_counts,
            "initial_api_rows": len(initial_entries),
            "final_api_rows": len(final_entries),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": initial_api_ids,
            "final_visible_ids": final_api_ids,
            "initial_ui_order_ids": initial_ui_ids,
            "final_ui_order_ids": final_ui_ids,
            "distinct_ui_title_rows": distinct_titles,
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "exact_apply_page_opened": exact_apply_url,
            "representative_apply_page_kB_id_verified": apply_ui_error is None,
            "representative_apply_ui_error": apply_ui_error,
            "description_source": "Each exact listed ID's kB single-job content field (same row the apply page fetches for its hero and the Greenhouse widget renders)",
            "source_has_structured_compensation_field": False,
            "salary_policy": "Structured pay_input_ranges retained when non-empty; the inspected kB rows expose no non-empty pay ranges, so salary stays null while compensation prose in full descriptions is preserved verbatim and never converted to structured salary.",
            "job_attachment_links": {str(job.job_id): (job.raw.get("source_ui") or {}).get("attachment_links") or []
                                     for job in ordered_jobs
                                     if (job.raw.get("source_ui") or {}).get("attachment_links")},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["detail_mapping_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
