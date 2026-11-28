"""Druva's own careers-page + Greenhouse board journey.

Observed 2026-11-07 at https://www.druva.com/about/careers (which 301s to
https://www.druva.com/why-druva/explore/careers, the canonical page):

* The entry page ships ``div.cmp-greenhouse-departments[data-board-token="druva"]``
  (the ``greenhouseDepartments`` AEM component) plus an inline
  ``generateMainPageSchema`` script that fetches the unfiltered
  ``https://boards-api.greenhouse.io/v1/boards/druva/jobs`` and builds a
  schema ``ItemList`` of ``<origin>/about/careers/jobs?gh_jid=<id>`` links.
* Its shipped component bundle (``clientlib-site`` JS) implements both
  ``greenhouseDepartments`` and ``greenhouseJobList``, each fetching
  ``https://boards-api.greenhouse.io/v1/boards/<token>/departments``.
  Department-tile URLs are generated as
  ``/content/druvaincprogram/us/en/about/careers/departments/<slug>.html``;
  those pages embed ``greenhouseJobList`` with ``data-department-filter``
  and render rows from ``absolute_url``/``location.name``.
* Job rows link to ``absolute_url``, i.e.
  ``https://www.druva.com/why-druva/explore/careers/jobs/<id>/?gh_jid=<id>``.
  That page embeds ``div#grnhse_app`` plus the Greenhouse embed script,
  and its own inline script fetches the exact detail
  ``https://boards-api.greenhouse.io/v1/boards/druva/jobs/<id>``. With
  ``gh_jid`` present the embed loads
  ``job-boards.greenhouse.io/embed/job_app?for=druva&token=<id>`` as the
  application iframe.

This adapter follows only those company-published requests: it derives the
board token and both listing endpoints from the official entry page and its
shipped component bundle, opens one representative exact detail page plus
its embed script, then fetches the listing snapshots and each exact
``/jobs/<id>`` detail the company journey exposes. It never enumerates a
different Greenhouse board.
"""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


BOARD_HOST = "boards-api.greenhouse.io"
EMBED_HOST = "boards.greenhouse.io"
JOB_ID = re.compile(r"^\d+$")
JOBS_LISTING = re.compile(
    r"https://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)/jobs"
)
DEPARTMENTS_TEMPLATE = re.compile(
    r"https://boards-api\.greenhouse\.io/v1/boards/\$\{([A-Za-z_$][A-Za-z0-9_$]*)\}/departments"
)
DETAIL_FETCH = re.compile(
    r"https://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)/jobs/\$\{jobId\}"
)
EMBED_SCRIPT = re.compile(
    r"https://boards\.greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)"
)
EMBED_APPLICATION_URI = re.compile(r"applicationURI:\s*'([^']+)'")
EMBED_TOKEN = re.compile(r"embedToken:\s*'([^']+)'")
PAY_RANGE = re.compile(
    r"The pay range for this position is expected to be between "
    r"\$[\d,]+(?:\.\d+)? and \$?[\d,]+(?:\.\d+)?/year"
)

PIPELINE_TITLE = re.compile(r"future opportunit", re.I)
PIPELINE_DEPT = re.compile(r"talent community", re.I)
NOT_OPEN = re.compile(r"not in reference to a currently open position", re.I)


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _salary_from_description(description_text):
    normalized = re.sub(r"\s+", " ", description_text or "")
    match = PAY_RANGE.search(normalized)
    if not match:
        return None
    return {
        "source_text": match.group(0),
        "source_field": "content",
        "semantics": (
            "Base pay range as published in the full description; the source "
            "states the total compensation package may also include incentive "
            "compensation and equity. Preserve text without conversion."
        ),
    }


def _is_pipeline(title, department_names, description_text):
    reasons = []
    if title and PIPELINE_TITLE.search(title):
        reasons.append("future_opportunity_title")
    if any(name and PIPELINE_DEPT.search(name) for name in department_names):
        reasons.append("talent_community_department")
    if description_text and NOT_OPEN.search(description_text):
        reasons.append("description_states_not_currently_open")
    return reasons


def _tile_counts(departments_payload):
    """Reproduce the entry-page ``greenhouseDepartments`` tile aggregation.

    The component maps ``parent_id`` through a fixed parent-name table,
    skips departments whose *name* mentions "Future Opportunities" or
    "Talent Community", and counts each department's embedded jobs.
    """
    parent_names = {
        "4049384002": "Sales", "4049345002": "Engineering",
        "4049363002": "Product", "4049375002": "Marketing",
        "4049796002": "G&A", "4049367002": "Customer Success",
    }
    tiles = {}
    skipped = {}
    for department in departments_payload.get("departments", []):
        jobs = department.get("jobs") or []
        if not jobs:
            continue
        name = department.get("name") or ""
        if "Future Opportunities" in name or "Talent Community" in name:
            skipped[name] = [str(job.get("id")) for job in jobs]
            continue
        key = parent_names.get(str(department.get("parent_id")), name)
        tiles[key] = tiles.get(key, 0) + len(jobs)
    return dict(sorted(tiles.items())), skipped


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _departments_url(jobs_listing_url):
    match = JOBS_LISTING.fullmatch(jobs_listing_url)
    if not match:
        raise ValueError(f"Druva jobs listing URL is not the observed Board endpoint: {jobs_listing_url}")
    return f"https://boards-api.greenhouse.io/v1/boards/{match.group(1)}/departments"


class Druva(Adapter):
    """Follow only Druva's official careers page and its published board."""

    VERIFICATION_REVISION = "druva.official-careers-greenhouse-board.1"
    EXPECTED_TOKEN = "druva"
    COMPANY_HOSTS = {"druva.com", "www.druva.com"}

    @staticmethod
    def _assert_company_url(url, *, path_prefix=None, label="company page"):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Druva.COMPANY_HOSTS:
            raise ValueError(f"Druva {label} left the official HTTPS site: {url}")
        if path_prefix is not None and not parsed.path.startswith(path_prefix):
            raise ValueError(f"Druva {label} has an unexpected path: {url}")
        return str(url)

    @staticmethod
    def _board_token_from_entry(html):
        soup = BeautifulSoup(html, "lxml")
        components = soup.select(
            'div[data-cmp-is="greenhouseDepartments"][data-board-token]'
        )
        if len(components) != 1:
            raise ValueError(
                "Druva official careers page does not expose exactly one "
                f"greenhouseDepartments component ({len(components)})"
            )
        component = components[0]
        token = (component.get("data-board-token") or "").strip()
        if token != Druva.EXPECTED_TOKEN:
            raise ValueError(f"Druva board token changed from 'druva': {token!r}")
        dept_template = soup.find("script", id="greenhouse-dept-tmpl")
        if dept_template is None or "this.count" not in dept_template.get_text():
            raise ValueError("Druva entry page lost its greenhouse-dept-tmpl Handlebars template")
        return token

    @staticmethod
    def _jobs_listing_from_entry(html, token):
        urls = sorted({match[0] for match in JOBS_LISTING.finditer(html)})
        exact = [url for url in urls
                 if url_split_host(url) == BOARD_HOST and not url.endswith("/departments")]
        if len(exact) != 1:
            raise ValueError(
                "Druva entry page does not expose exactly one unfiltered "
                f"greenhouse jobs listing request ({exact})"
            )
        url = exact[0]
        match = JOBS_LISTING.fullmatch(url)
        if not match or match.group(1) != token:
            raise ValueError(f"Druva entry page exposes an unexpected jobs listing board: {url}")
        return url

    async def _components_script(self, entry_url, html):
        soup = BeautifulSoup(html, "lxml")
        candidates = [urljoin(entry_url, node.get("src", ""))
                      for node in soup.select("script[src]")
                      if "clientlib-site" in (node.get("src") or "")
                      and (node.get("src") or "").endswith(".js")]
        candidates = sorted(set(candidates))
        if len(candidates) != 1:
            raise ValueError(
                f"Druva entry page does not expose exactly one clientlib-site component bundle ({candidates})"
            )
        script_url = self._assert_company_url(candidates[0], path_prefix="/etc.clientlibs/", label="component bundle")
        response = await self.http.get(script_url)
        final = self._assert_company_url(str(response.url), path_prefix="/etc.clientlibs/", label="component bundle")
        return final, response.text

    @staticmethod
    def _departments_endpoint(script, token, job_list_url):
        if "greenhouseDepartments" not in script or "greenhouseJobList" not in script:
            raise ValueError("Druva component bundle no longer ships the greenhouseDepartments/greenhouseJobList components")
        templates = sorted({match[0] for match in DEPARTMENTS_TEMPLATE.finditer(script)})
        derived = sorted({template.replace("${" + match.group(1) + "}", token)
                          for template in templates
                          for match in [DEPARTMENTS_TEMPLATE.fullmatch(template)]})
        expected = _departments_url(job_list_url)
        if derived != [expected]:
            raise ValueError(f"Druva component bundle departments request shape changed: {templates}")
        return derived[0]

    @staticmethod
    def _listing_jobs(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"Druva {snapshot_name} jobs-listing response has no jobs array")
        rows = payload["jobs"]
        for row in rows:
            identity = row.get("id") if isinstance(row, dict) else None
            if not isinstance(identity, int) or not JOB_ID.fullmatch(str(identity)):
                raise ValueError(f"Druva {snapshot_name} listing contains a missing or invalid exact job ID: {identity!r}")
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Druva {snapshot_name} listing repeats a company-published ID")
        titles = [row.get("title") for row in rows]
        if any(not isinstance(title, str) or not title.strip() for title in titles):
            raise ValueError(f"Druva {snapshot_name} listing contains an unidentified job")
        return rows

    @staticmethod
    def _department_jobs(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
            raise ValueError(f"Druva {snapshot_name} departments response has no departments array")
        by_id = {}
        for department in payload["departments"]:
            if not isinstance(department, dict):
                continue
            for row in department.get("jobs") or []:
                identity = row.get("id")
                if not isinstance(identity, int) or not JOB_ID.fullmatch(str(identity)):
                    raise ValueError(
                        f"Druva {snapshot_name} departments payload carries a missing or invalid job ID: {identity!r}"
                    )
                by_id.setdefault(identity, row)
        return by_id

    async def _listing_snapshot(self, url, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"Druva {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Druva {label} listing response is not JSON") from exc
        return payload, response.text

    async def _detail_snapshot(self, detail_url, identity):
        response = await self.http.get(detail_url)
        if str(response.url) != detail_url:
            raise ValueError("Druva exact job detail request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Druva job {identity} detail response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("id") != identity:
            raise ValueError(f"Druva job {identity} detail response identity differs from its listed ID")
        return payload

    def _make_job(self, row, detail, *, jobs_url, detail_comment_url, embed_script_url,
                  embed_token, application_uri):
        identity = row["id"]
        title = detail.get("title") or row.get("title") or ""
        if title != row.get("title"):
            raise ValueError(f"Druva listed ID {identity} title differs between listing and detail")
        company_name = detail.get("company_name") or row.get("company_name")
        if company_name != "Druva":
            raise ValueError(f"Druva listed ID {identity} has an unexpected company_name: {company_name!r}")
        job_url = row.get("absolute_url")
        expected_url = f"https://www.druva.com/why-druva/explore/careers/jobs/{identity}/?gh_jid={identity}"
        if job_url != expected_url or detail.get("absolute_url") != expected_url:
            raise ValueError(f"Druva listed ID {identity} has an unexpected company-published job URL")
        apply_url = (
            f"{application_uri}&token={identity}&validityToken={embed_token}"
            if application_uri and embed_token else None
        )
        description_html = detail.get("content") or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Druva public detail content is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Druva public detail description is implausibly short")
        date_posted = detail.get("first_published")
        if not isinstance(date_posted, str) or not date_posted.strip():
            date_posted = None
        offices = detail.get("offices") or []
        department_names = [d.get("name") for d in detail.get("departments") or []]
        locations = [{"location": office.get("name"), "address": office.get("location")}
                     for office in offices if office.get("name")]
        if not locations and detail.get("location", {}).get("name"):
            locations = [{"location": detail["location"]["name"], "address": None}]
        salary = _salary_from_description(description_text)
        pipeline = _is_pipeline(title, department_names, description_text)
        raw = {
            "listing_row": row,
            "detail": detail,
            "source_ui": {
                "official_job_url": job_url,
                "rendered_apply_url": apply_url,
                "embed_script_url": embed_script_url,
                "listing_jobs_url": jobs_url,
                "detail_url": detail_comment_url,
                "department_names": department_names,
                "offices": offices,
                "metadata": detail.get("metadata"),
                "internal_job_id": detail.get("internal_job_id"),
                "application_deadline": detail.get("application_deadline"),
                "attachment_links": _attachment_links(description_html),
                "salary_from_description": salary,
                "pipeline_flags": pipeline,
            },
        }
        job = self.job(
            raw,
            title=title,
            url=job_url,
            job_id=str(identity),
            requisition_id=str(detail.get("requisition_id"))
            if detail.get("requisition_id") is not None else None,
            apply_url=apply_url,
            description=description_html,
            method="druva.official_careers_detail.greenhouse_job_content",
            description_url=detail_comment_url,
            locations=locations,
            department=department_names[0] if len(department_names) == 1 else (
                department_names or None),
            team=None,
            employment_type=None,
            workplace_type=None,
            employer=company_name,
            date_posted=date_posted,
            date_posted_raw=detail.get("first_published"),
            date_updated=detail.get("updated_at"),
            valid_through=detail.get("application_deadline"),
            salary=salary,
        )
        job.description_text = description_text
        job.record_identity = f"druva:{identity}"
        if pipeline:
            job.warnings.append("pipeline_or_future_opportunity_posting")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url_requested = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url_requested)
        if parts.scheme != "https" or parts.hostname not in self.COMPANY_HOSTS \
                or parts.path.rstrip("/") != "/about/careers":
            raise ValueError("Druva dedicated flow must start from its official HTTPS /about/careers entry")
        entry = await self.http.get(entry_url_requested)
        entry_url = self._assert_company_url(
            str(entry.url), path_prefix="/why-druva/explore/careers", label="official entry")
        token = self._board_token_from_entry(entry.text)
        jobs_url = self._jobs_listing_from_entry(entry.text, token)
        departments_url = None
        component_url = None
        component_error = None
        component_url, script = await self._components_script(entry_url, entry.text)
        departments_url = self._departments_endpoint(script, token, jobs_url)

        initial_jobs_payload, _ = await self._listing_snapshot(jobs_url, "initial")
        initial_job_rows = self._listing_jobs(initial_jobs_payload, "initial")
        initial_by_id = {row["id"]: row for row in initial_job_rows}
        initial_dept_payload, _ = await self._listing_snapshot(departments_url, "initial")
        initial_by_dept = self._department_jobs(initial_dept_payload, "initial")
        if not initial_job_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Druva official board returned zero visible jobs; empty state requires independent review")
            return self.result
        if set(initial_by_dept) != set(initial_by_id):
            self.result.errors.append(
                "druva_listing_membership_differs_between_jobs_and_departments_snapshots:"
                f" jobs_only={sorted(set(initial_by_id) - set(initial_by_dept))}"
                f" departments_only={sorted(set(initial_by_dept) - set(initial_by_id))}")

        # Representative exact detail page (first listed order) + its embed.
        exact_detail_page_url = initial_job_rows[0]["absolute_url"]
        detail_started = time.perf_counter()
        detail_ui_error = None
        embed_script_url = None
        embed_token = None
        application_uri = None
        try:
            detail_page = await self.http.get(exact_detail_page_url)
            final_detail_page = self._assert_company_url(
                str(detail_page.url), path_prefix="/why-druva/explore/careers/jobs/", label="exact detail page")
            # The absolute_url keeps a trailing slash; the site 301s it to the
            # no-slash variant. Accept exactly that normalization.
            if final_detail_page not in (exact_detail_page_url,
                                          exact_detail_page_url.replace("/?gh_jid", "?gh_jid")):
                raise ValueError("Druva exact detail page redirected to an unexpected URL")
            soup = BeautifulSoup(detail_page.text, "lxml")
            if soup.find(id="grnhse_app") is None:
                raise ValueError("Druva exact detail page lost its grnhse_app embed container")
            embeds = sorted({match[0] for match in EMBED_SCRIPT.finditer(detail_page.text)})
            if len(embeds) != 1 or EMBED_SCRIPT.fullmatch(embeds[0]).group(1) != token:
                raise ValueError(f"Druva exact detail page embed script changed: {embeds}")
            embeds_match = DETAIL_FETCH.search(detail_page.text)
            if not embeds_match or embeds_match.group(1) != token:
                raise ValueError("Druva exact detail page no longer fetches its exact job by gh_jid")
            embed_script_url = embeds[0]
            embed_page = await self.http.get(embed_script_url)
            embed_text = embed_page.text
            match = EMBED_APPLICATION_URI.search(embed_text)
            token_match = EMBED_TOKEN.search(embed_text)
            if not match or not token_match:
                raise ValueError("Druva greenhouse embed script changed its applicationURI/embedToken")
            application_uri = match.group(1)
            embed_token = token_match.group(1)
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(
                f"Druva representative exact detail page {exact_detail_page_url}: {detail_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        # Hydrate every exact detail the company journey lists.
        mapping_started = time.perf_counter()
        details = {}
        for row in initial_job_rows:
            identity = row["id"]
            try:
                detail_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{identity}"
                details[identity] = await self._detail_snapshot(detail_url, identity)
            except Exception as exc:
                self.result.errors.append(f"{identity}: Druva exact detail fetch failed: {type(exc).__name__}: {exc}")
        detail_mapping_seconds = time.perf_counter() - mapping_started

        final_snapshot_verified = True
        try:
            final_jobs_payload, _ = await self._listing_snapshot(jobs_url, "final")
            final_job_rows = self._listing_jobs(final_jobs_payload, "final")
            final_dept_payload, _ = await self._listing_snapshot(departments_url, "final")
            final_by_dept = self._department_jobs(final_dept_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_job_rows = initial_job_rows
            final_dept_payload = initial_dept_payload
            final_by_dept = initial_by_dept
            self.result.errors.append(
                "Druva final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}")
        final_by_id = {row["id"]: row for row in final_job_rows}
        changed_membership = final_snapshot_verified and set(final_by_id) != set(initial_by_id)
        if changed_membership:
            self.result.errors.append("druva_official_listing_membership_changed_between_initial_and_final_snapshots")
        if final_snapshot_verified and set(final_by_id) != set(final_by_dept):
            self.result.errors.append(
                "druva_final_membership_differs_between_jobs_and_departments_snapshots:"
                f" jobs_only={sorted(set(final_by_id) - set(final_by_dept))}"
                f" departments_only={sorted(set(final_by_dept) - set(final_by_id))}")

        all_ids = list(dict.fromkeys([row["id"] for row in final_job_rows]
                                     + [row["id"] for row in initial_job_rows]))
        for identity in all_ids:
            row = final_by_id.get(identity) or initial_by_id.get(identity)
            if row is None or identity not in details:
                continue
            self.add(self._make_job(
                row, details[identity], jobs_url=jobs_url,
                detail_comment_url=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{identity}",
                embed_script_url=embed_script_url, embed_token=embed_token,
                application_uri=application_uri,
            ))

        tiles, skipped_talent = _tile_counts(final_dept_payload)
        pipeline_ids = sorted({job.job_id for job in self.result.jobs
                                 if "pipeline_or_future_opportunity_posting" in job.warnings})
        self.result.expected_total = len(final_job_rows)
        self.result.pages = 1
        if changed_membership or set(final_by_id) != set(final_by_dept):
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url_requested": entry_url_requested,
            "official_entry_url": entry_url,
            "membership_authority": (
                "Druva official /about/careers page (canonical "
                "/why-druva/explore/careers): its greenhouseDepartments component "
                "data-board-token and its inline unfiltered "
                "boards-api.greenhouse.io/v1/boards/druva/jobs fetch, reconciled "
                "against the components' boards/druva/departments payload."
            ),
            "board_token": token,
            "component_bundle_url": component_url,
            "component_bundle_error": component_error,
            "listing_jobs_url": jobs_url,
            "listing_departments_url": departments_url,
            "membership_source": "boards_api_unfiltered_jobs_list + departments aggregate",
            "ui_pagination": "None; both endpoints return the full board in one response.",
            "department_filters": "Department tiles generated client-side; counts exclude "
                                  "departments named '*Future Opportunities*' or '*Talent Community*'.",
            "initial_visible_ids": [row["id"] for row in initial_job_rows],
            "final_visible_ids": [row["id"] for row in final_job_rows],
            "initial_departments_job_ids": sorted(initial_by_dept),
            "final_departments_job_ids": sorted(final_by_dept),
            "department_tile_counts_observed": tiles,
            "department_tile_skipped_names": skipped_talent,
            "pipeline_or_future_opportunity_job_ids": pipeline_ids,
            "exact_native_detail_page_opened": exact_detail_page_url,
            "representative_detail_page_fetches_exact_job_by_gh_jid": detail_ui_error is None,
            "representative_detail_ui_error": detail_ui_error,
            "description_source": "Each exact listed job's /boards/druva/jobs/<id> detail 'content'.",
            "salary_policy": "Exact 'The pay range...' sentence retained with source semantics when published; no conversion.",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 4,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result


def url_split_host(url):
    return (urlsplit(url).hostname or "").lower()
