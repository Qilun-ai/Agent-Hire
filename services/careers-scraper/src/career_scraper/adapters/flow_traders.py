"""Flow Traders's own Greenhouse-backed careers listing and detail journey.

Official entry ``https://www.flowtraders.com/careers/`` links to
``/careers/job-search/`` ("Explore all our roles"). That listing page mounts a
Vue app whose shipped ``/js/job-search.js`` fetches Flow Traders's public
Greenhouse board ``flowtraders`` with ``content=true&per_page=500`` plus an
offices feed for its client-side location filter. Cards link to Flow Traders's
own server-rendered ``/careers/job-description/<id>`` page; the apply link
template resolves to ``/careers/job-application/<id>``.

Membership authority is the unfiltered Greenhouse ``jobs`` array behind that
company-published listing component. Filters (keyword title, Division
metadata, office names) and "Show more" paging (10 at a time) are client-side
slices over the single 500-row board response; there is no server-side
pagination to follow. Descriptions are verified against two exact native
detail pages whose server-rendered description block is byte-identical to the
decoded listing ``content`` on the sampled date.
"""

import hashlib
import html as _html
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


BOARD_HOST = "boards-api.greenhouse.io"
BOARD_SLUG = "flowtraders"
LISTING_API_URL = (
    f"https://{BOARD_HOST}/v1/boards/{BOARD_SLUG}/jobs?content=true&per_page=500"
)
OFFICES_API_URL = f"https://{BOARD_HOST}/v1/boards/{BOARD_SLUG}/offices"
ENTRY_URL = "https://www.flowtraders.com/careers/"
LISTING_PAGE_URL = "https://www.flowtraders.com/careers/job-search/"
JOB_SEARCH_JS_URL = "https://www.flowtraders.com/js/job-search.js"
CONSTANTS_JS_URL = "https://www.flowtraders.com/js/constants.js"
DETAIL_BASE_URL = "https://www.flowtraders.com/careers/job-description/"
APPLICATION_BASE_URL = "https://www.flowtraders.com/careers/job-application/"
LISTED_JOB_URL_PREFIX = f"https://job-boards.greenhouse.io/{BOARD_SLUG}/jobs/"

# Exact fetch strings shipped in /js/job-search.js on the inspected date.
FETCH_PATTERN = re.compile(
    r"fetch\s*\(\s*([`'\"])(https://boards-api\.greenhouse\.io/v1/boards/flowtraders/(?:jobs\?content=true&per_page=500|offices))\1"
)
# Vue listing card template: "'/' + slug + '/' + JOB_DESCRIPTION_ROUTE + '/' + job.id"
CARD_ROUTE_TOKENS = ("JOB_DESCRIPTION_ROUTE", "job.id", "slug")
CARD_DISPLAY_TECHNOLOGY_SET = {"it", "development", "quality assurance"}


def compact_body_hash(html_value):
    """Mirror the Atlan NFC/whitespace-free body hash for cross-checking."""
    value = unicodedata.normalize("NFC", text_html(html_value))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decode_content(row_content):
    """Decode one Greenhouse whole-wrapper escape into normal HTML.

    Greenhouse ``content`` is HTML-escaped once as a whole (``&lt;p&gt;`` ...)
    with inner entities double-escaped (``&amp;amp;`` for a literal ``&amp;``).
    A single :func:`html.unescape` restores exactly the server-rendered detail
    page ``<div id="description">`` block on the sampled date.
    """
    return _html.unescape(row_content or "")


def native_job_url(identity):
    identity_text = str(identity).strip()
    if not identity_text.isdigit() or int(identity_text) <= 0:
        raise ValueError(f"Flow Traders listed job ID is not a numeric Greenhouse ID: {identity!r}")
    return DETAIL_BASE_URL + identity_text


def application_url(identity):
    identity_text = str(identity).strip()
    if not identity_text.isdigit() or int(identity_text) <= 0:
        raise ValueError(f"Flow Traders listed job ID is not a numeric Greenhouse ID: {identity!r}")
    return APPLICATION_BASE_URL + identity_text


def listed_job_url(identity):
    identity_text = str(identity).strip()
    if not identity_text.isdigit() or int(identity_text) <= 0:
        raise ValueError(f"Flow Traders listed job ID is not a numeric Greenhouse ID: {identity!r}")
    return LISTED_JOB_URL_PREFIX + identity_text


def card_department_label(department_name):
    """Reproduce the listing Vue card's display mapping for departments.

    job-search.js renders ``Technology`` when departments[0].name lowercases
    into IT/development/quality assurance, otherwise the raw name. The detail
    page pills instead render the raw department; both labels are retained
    separately in ``source_ui`` so neither display rule overwrites source data.
    """
    name = department_name or ""
    return "Technology" if name.lower() in CARD_DISPLAY_TECHNOLOGY_SET else name


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def extract_description_block(detail_html):
    """Return the server-rendered #description block inner HTML (stripped).

    The ``<div id="description">`` wrapper ends with the ``Apply now``
    ``<a id="application-link">`` child. That action link is not part of the
    Greenhouse ``content`` body, so it is decomposed before comparison. Only
    outer whitespace is stripped; interior HTML must match the decoded
    listing content exactly.
    """
    soup = BeautifulSoup(detail_html or "", "lxml")
    node = soup.select_one("div#description.job-description")
    if node is None:
        raise ValueError("Flow Traders native detail page has no #description.job-description block")
    link = node.select_one("a#application-link")
    if link is not None:
        link.decompose()
    return node.decode_contents().strip()


def extract_detail_title(detail_html):
    soup = BeautifulSoup(detail_html or "", "lxml")
    heading = soup.select_one("h1")
    if heading is None:
        raise ValueError("Flow Traders native detail page has no h1 title")
    return heading.get_text(" ", strip=True).strip()


def extract_detail_pills(detail_html):
    """Return the three job-header pills: location, division, department.

    Only header pills carry ``smallerPadding``. The page also contains a
    share-ticker rounded-full chip in the nav and black rounded social-icon
    circles; both are excluded. A blank middle pill is the observed Events
    rendering (division Events renders empty); it is preserved as ``""``.
    """
    soup = BeautifulSoup(detail_html or "", "lxml")
    pills = []
    for div in soup.select("div.rounded-full.smallerPadding"):
        text = div.get_text(" ", strip=True).strip()
        pills.append(text)
    return pills


def extract_application_template(detail_html):
    """Return the inline apply-href template ending in /<id>."""
    match = re.search(r"anchor\.href\s*=\s*`([^`]*JOB_APPLICATION_ROUTE[^`]*)`", detail_html or "")
    if not match:
        raise ValueError("Flow Traders native detail page no longer sets #application-link from JOB_APPLICATION_ROUTE")
    return match.group(1).strip()


class FlowTraders(Adapter):
    """Collect only the jobs behind Flow Traders's published careers UI."""

    VERIFICATION_REVISION = "flow-traders.greenhouse-board-list-and-detail-exact.1"
    CAREERS_HOSTS = {"www.flowtraders.com", "flowtraders.com"}

    @staticmethod
    def _assert_careers_url(url, *, path_prefix="/careers"):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in FlowTraders.CAREERS_HOSTS:
            raise ValueError(f"Flow Traders company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Flow Traders company page asset has an unexpected path: {url}")

    @staticmethod
    def _board_fetch_urls(script, label):
        urls = sorted({match[2] for match in FETCH_PATTERN.finditer(script or "")})
        expected = sorted({LISTING_API_URL, OFFICES_API_URL})
        if urls != expected:
            raise ValueError(
                f"Flow Traders {label} component does not expose exactly its inspected "
                f"Greenhouse board fetches: {urls!r}"
            )
        return LISTING_API_URL, OFFICES_API_URL

    @staticmethod
    def _assert_listing_script(script):
        required = (
            "boards-api.greenhouse.io",
            "/v1/boards/flowtraders/jobs",
            "content=true&per_page=500",
            "/v1/boards/flowtraders/offices",
            "allJobs",
            "filteredJobs",
            "showMoreJobs",
            "numberOfJobsDisplayed",
            "filterByKeyword",
            "filterByDepartments",
            "filterByOffices",
            "metadata.find((x) => x.name.toLowerCase() === 'division')",
        )
        normalized = re.sub(r"\s+", " ", script or "")
        if any(token not in script and token not in normalized for token in required):
            raise ValueError("Flow Traders job-search.js no longer matches its inspected list/filter flow")
        if "JOB_DESCRIPTION_ROUTE" not in script:
            raise ValueError("Flow Traders job-search.js no longer exposes its native detail route constant")

    @staticmethod
    def _assert_listing_page(page_html, page_url):
        soup = BeautifulSoup(page_html or "", "lxml")
        if soup.select_one("#app") is None:
            raise ValueError("Flow Traders listing page no longer mounts the #app Vue listing")
        script_srcs = [node.get("src", "") for node in soup.select("script[src]")]
        if "/js/job-search.js" not in script_srcs:
            raise ValueError("Flow Traders listing page no longer loads /js/job-search.js")
        text = page_html or ""
        if "JOB_DESCRIPTION_ROUTE" not in text or "About this role" not in text:
            raise ValueError("Flow Traders listing page no longer exposes its About this role card route")

    @staticmethod
    def _assert_constants(script):
        values = {}
        for name in ("HOME_PAGE_NAME", "CAREERS_ROUTE", "JOB_DESCRIPTION_ROUTE", "JOB_APPLICATION_ROUTE"):
            match = re.search(rf"export\s+const\s+{name}\s*=\s*'([^']+)'", script or "")
            if not match:
                raise ValueError(f"Flow Traders constants.js no longer exports {name}")
            values[name] = match.group(1)
        if values.get("CAREERS_ROUTE") != "careers":
            raise ValueError("Flow Traders CAREERS_ROUTE changed from its inspected 'careers' value")
        if values.get("JOB_DESCRIPTION_ROUTE") != "job-description":
            raise ValueError("Flow Traders JOB_DESCRIPTION_ROUTE changed from its inspected 'job-description' value")
        if values.get("JOB_APPLICATION_ROUTE") != "job-application":
            raise ValueError("Flow Traders JOB_APPLICATION_ROUTE changed from its inspected 'job-application' value")
        return values

    @staticmethod
    def _division_value(row):
        metadata = row.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict) and str(item.get("name") or "").lower() == "division":
                value = str(item.get("value")).strip() if item.get("value") is not None else ""
                # job-search.js replaces a missing/empty Division with "No division"
                # before rendering/filtering; mirror that display value so the UI
                # filter value is retained separately from the raw row.
                return value or "No division"
        return "No division"

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"Flow Traders {snapshot_name} listing response has no jobs array")
        rows = payload["jobs"]
        meta = payload.get("meta") or {}
        total = meta.get("total") if isinstance(meta, dict) else None
        if total is not None and total != len(rows):
            raise ValueError(
                f"Flow Traders {snapshot_name} listing reports total={total!r} but contains {len(rows)} rows; "
                "the 500-row company fetch does not cover membership"
            )
        if total is None:
            raise ValueError(f"Flow Traders {snapshot_name} listing response has no meta.total")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Flow Traders {snapshot_name} listing contains a non-object row")
            identity = row.get("id")
            if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
                raise ValueError(f"Flow Traders {snapshot_name} listing contains a missing or invalid job ID: {identity!r}")
            identity_text = str(identity)
            if identity_text in seen:
                raise ValueError(f"Flow Traders {snapshot_name} listing repeats job ID {identity_text}")
            seen.add(identity_text)
            if not (row.get("title") or str(row.get("title") or "").strip()):
                raise ValueError(f"Flow Traders {snapshot_name} listing contains an unidentified job {identity_text}")
            location = row.get("location") or {}
            if not isinstance(location, dict) or location.get("name") is None:
                raise ValueError(f"Flow Traders {snapshot_name} listing job {identity_text} has no location name")
            departments = row.get("departments") or []
            if not departments or not isinstance(departments[0], dict) or not departments[0].get("name"):
                raise ValueError(f"Flow Traders {snapshot_name} listing job {identity_text} has no department name")
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"Flow Traders {snapshot_name} listing job {identity_text} has no content field")
            description_html = decode_content(content)
            if not description_html.strip():
                raise ValueError(f"Flow Traders {snapshot_name} listing job {identity_text} decodes to an empty description")
            if len(text_html(description_html).strip()) < 100:
                raise ValueError(f"Flow Traders {snapshot_name} listing job {identity_text} description is implausibly short")
        return list(rows)

    @staticmethod
    def _offices(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("offices"), list):
            raise ValueError("Flow Traders offices response has no offices array")
        offices = payload["offices"]
        if not any(isinstance(office, dict) and office.get("location") for office in offices):
            raise ValueError("Flow Traders offices response lists no office with a location")
        return offices

    def _make_job(self, row, *, listing_api_url, offices_api_url, detail_verified=False,
                  initial_row=None, membership_changed=False):
        identity = str(row["id"])
        expected_listed_url = listed_job_url(identity)
        if row.get("absolute_url") != expected_listed_url:
            raise ValueError(f"Flow Traders listed ID {identity} has an unexpected Greenhouse absolute_url")
        description_html = decode_content(row.get("content"))
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Flow Traders public detail content is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Flow Traders public detail description is implausibly short")
        location_name = row["location"]["name"]
        offices = row.get("offices") or []
        department_name = row["departments"][0]["name"]
        division = self._division_value(row)
        first_published = row.get("first_published")
        # Source-only dates. Never use Start Date metadata, updated_at, or
        # request time as the posting date. Unknown stays null.
        date_posted = first_published if isinstance(first_published, str) and first_published.strip() else None
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": native_job_url(identity),
                "listed_absolute_url": row["absolute_url"],
                "rendered_apply_url": application_url(identity),
                "listing_card_department_label": card_department_label(department_name),
                "division_value": division,
                "listing_api_url": listing_api_url,
                "offices_api_url": offices_api_url,
                "attachment_links": attachment_links,
                "salary_from_description": None,
                "detail_page_verified": bool(detail_verified),
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=native_job_url(identity),
            job_id=identity,
            requisition_id=str(row["requisition_id"]) if row.get("requisition_id") is not None else None,
            apply_url=application_url(identity),
            description=description_html,
            method="flow-traders.greenhouse-board.content",
            description_url=listing_api_url,
            locations=[{"location": location_name, "offices": offices}],
            department=department_name,
            team=division,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            valid_through=row.get("application_deadline"),
            salary=None,
            employer=row.get("company_name"),
            language=row.get("language"),
        )
        job.description_text = description_text
        job.record_identity = f"flow-traders:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        if urlunsplit(urlsplit(entry_url)[:2] + ("/careers/", "", "")) != ENTRY_URL and entry_url != ENTRY_URL:
            # Accept the canonical entry exactly; normalize a missing trailing
            # slash is not allowed because the detail slug depends on it.
            if str(entry_url).rstrip("/") != ENTRY_URL.rstrip("/"):
                raise ValueError("Flow Traders dedicated flow must start from its official HTTPS /careers/ entry")
        entry = await self.http.get(entry_url)
        self._assert_careers_url(entry.url, path_prefix="/careers")
        if str(entry.url) != entry_url:
            raise ValueError("Flow Traders official entry redirected away from its canonical careers URL")
        if "/careers/job-search/" not in (entry.text or ""):
            raise ValueError("Flow Traders /careers/ entry no longer links to /careers/job-search/")

        listing_page = await self.http.get(LISTING_PAGE_URL)
        self._assert_careers_url(listing_page.url, path_prefix="/careers/job-search")
        if str(listing_page.url) != LISTING_PAGE_URL:
            raise ValueError("Flow Traders listing page redirected away from its canonical URL")
        self._assert_listing_page(listing_page.text, str(listing_page.url))

        job_search_response = await self.http.get(JOB_SEARCH_JS_URL)
        self._assert_careers_url(job_search_response.url, path_prefix="/js/")
        listing_api_url, offices_api_url = self._board_fetch_urls(job_search_response.text, "job-search.js")
        self._assert_listing_script(job_search_response.text)
        constants_response = await self.http.get(CONSTANTS_JS_URL)
        self._assert_careers_url(constants_response.url, path_prefix="/js/")
        constants = self._assert_constants(constants_response.text)

        initial_payload = await self._listing_snapshot(listing_api_url, "initial")
        initial_rows = self._rows(initial_payload, "initial")
        initial_by_id = {str(row["id"]): row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Flow Traders official embedded listing returned zero jobs; empty state requires independent review"
            )
            return self.result

        offices_started = time.perf_counter()
        offices_response = await self.http.get(offices_api_url)
        if str(offices_response.url) != offices_api_url:
            raise ValueError("Flow Traders offices request redirected away from its inspected source")
        try:
            offices_payload = offices_response.json()
        except ValueError as exc:
            raise ValueError("Flow Traders offices response is not JSON") from exc
        offices = self._offices(offices_payload)

        # Representative native details: first card in company UI order (API
        # order; job-search.js does not reorder allJobs) plus a second exact
        # card from a different primary location, to prove listing content
        # equals the server-rendered detail body beyond one pill variant.
        first_identity = str(initial_rows[0]["id"])
        second_identity = self._second_representative_id(initial_rows, first_identity)
        detail_started = time.perf_counter()
        detail_results = []
        detail_errors = []
        for sample_identity in dict.fromkeys([first_identity, second_identity]):
            detail_url = native_job_url(sample_identity)
            try:
                detail_page = await self.http.get(detail_url)
                self._assert_careers_url(detail_page.url, path_prefix="/careers/job-description/")
                if str(detail_page.url) != detail_url:
                    raise ValueError("Flow Traders native detail route redirected to a different page")
                detail_html = detail_page.text
                block = extract_description_block(detail_html)
                title = extract_detail_title(detail_html)
                pills = extract_detail_pills(detail_html)
                template = extract_application_template(detail_html)
                row = initial_by_id[sample_identity]
                expected_block = decode_content(row.get("content")).strip()
                # BeautifulSoup re-serializes void tags (<br/> vs <br>) and
                # entities (&nbsp; vs raw NBSP), so compare the normalized
                # compact text hash, not raw serializer bytes. The hash uses
                # the same NFC/whitespace-free text the verifier rechecks.
                if compact_body_hash(block) != compact_body_hash(expected_block):
                    raise ValueError("native detail #description block differs from decoded listing content")
                if text_html(block) != text_html(expected_block):
                    raise ValueError("native detail #description text differs from decoded listing content")
                if title.strip() != str(row.get("title") or "").strip():
                    raise ValueError("native detail h1 title differs from the exact listed title")
                if template != f"/${{urlSplit[3]}}/${{JOB_APPLICATION_ROUTE}}/{sample_identity}":
                    raise ValueError("native detail apply template changed from its inspected form")
                detail_results.append({
                    "job_id": sample_identity,
                    "url": detail_url,
                    "title_stripped": title.strip(),
                    "pills": pills,
                    "body_compact_sha256": compact_body_hash(block),
                    "apply_template": template,
                })
            except Exception as exc:
                detail_errors.append(f"{sample_identity}: {type(exc).__name__}: {exc}")
                self.result.errors.append(
                    f"Flow Traders representative exact detail page {detail_url}: {type(exc).__name__}: {exc}"
                )
        detail_ui_seconds = time.perf_counter() - detail_started
        verified_detail_ids = {item["job_id"] for item in detail_results}

        final_snapshot_verified = True
        try:
            final_payload = await self._listing_snapshot(listing_api_url, "final")
            final_rows = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "Flow Traders final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {str(row["id"]): row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("flow_traders_official_listing_membership_changed_between_initial_and_final_snapshots")

        # Keep final source records for stable IDs. On a moving listing, retain
        # rows seen in either snapshot and annotate stale initial-only rows.
        all_ids = list(dict.fromkeys(
            [str(row["id"]) for row in final_rows] + [str(row["id"]) for row in initial_rows]
        ))
        changed_rows = sorted(
            identity for identity in final_by_id.keys() & initial_by_id.keys()
            if final_by_id[identity] != initial_by_id[identity]
        )
        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, listing_api_url=listing_api_url, offices_api_url=offices_api_url,
                detail_verified=identity in verified_detail_ids,
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

        ui_order_ids = [str(row["id"]) for row in final_rows]
        # Attach the observed native detail evidence to the two sampled jobs'
        # raw source_ui for field-level audit.
        sampled_by_id = {item["job_id"]: item for item in detail_results}
        for job in self.result.jobs:
            if job.job_id in sampled_by_id:
                job.raw["source_ui"]["native_detail"] = sampled_by_id[job.job_id]
        offices_with_location = [
            office for office in offices if isinstance(office, dict) and office.get("location")
        ]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_page_url": LISTING_PAGE_URL,
            "listing_script_url": JOB_SEARCH_JS_URL,
            "constants_script_url": CONSTANTS_JS_URL,
            "listing_api_url": listing_api_url,
            "offices_api_url": offices_api_url,
            "constants": constants,
            "membership_authority": (
                "Flow Traders /careers/job-search/ Vue listing fetching the exact public "
                "Greenhouse board 'flowtraders' with content=true&per_page=500; all rows in "
                "that single board response are membership. Division/keyword/office filters "
                "and Show-more paging are client-side over that array."
            ),
            "listing_filter_behavior": (
                "unfiltered board rows are shown in API order; keyword matches title "
                "substring, Divisions matches metadata Division value, Locations matches "
                "offices[].name plus location prefix; Show more slices 10 more filtered rows"
            ),
            "ui_pagination": (
                "No server-side pagination; per_page=500 single fetch. Nearby Show more "
                "button is client-side slicing of filteredJobs at 10 rows."
            ),
            "division_filter_values": sorted({self._division_value(row) for row in final_rows}),
            "office_filter_values": sorted({
                str(office["name"]) for office in offices_with_location if isinstance(office, dict) and office.get("name")
            }),
            "initial_api_rows": len(initial_payload["jobs"]),
            "final_api_rows": len(final_payload["jobs"]),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [str(row["id"]) for row in initial_rows],
            "final_visible_ids": ui_order_ids,
            "source_rows_changed_between_snapshots": changed_rows,
            "representative_detail_pages": detail_results,
            "representative_detail_errors": detail_errors,
            "representative_detail_bodies_verified": len(detail_results),
            "exact_native_detail_pages_opened": [item["url"] for item in detail_results],
            "description_source": (
                "Each exact listed row's decoded content (single unescape) equals the "
                "server-rendered /careers/job-description/<id> #description block; that "
                "decoded HTML is retained as description_html."
            ),
            "source_has_structured_compensation_field": False,
            "salary_policy": (
                "No numeric structured compensation is exposed; generic 'competitive "
                "salary' prose is retained inside the full description only and salary "
                "stays null."
            ),
            "job_attachment_links": {
                str(row["id"]): _job_attachment_links(decode_content(row.get("content")))
                for row in final_rows if _job_attachment_links(decode_content(row.get("content")))
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
            "offices_fetch_seconds": round(time.perf_counter() - offices_started, 6),
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    @staticmethod
    def _second_representative_id(rows, first_identity):
        first_row = next((row for row in rows if str(row["id"]) == first_identity), None)
        first_location = (first_row.get("location") or {}).get("name") if first_row else None
        first_division = FlowTraders._division_value(first_row) if first_row else ""
        for row in rows:
            identity = str(row["id"])
            if identity == first_identity:
                continue
            location = (row.get("location") or {}).get("name")
            division = FlowTraders._division_value(row)
            if location != first_location or division != first_division:
                return identity
        fallback = str(rows[1]["id"]) if len(rows) > 1 else first_identity
        return fallback

    async def _listing_snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Flow Traders {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Flow Traders {label} listing response is not JSON") from exc
        self._rows(payload, label)
        return payload
