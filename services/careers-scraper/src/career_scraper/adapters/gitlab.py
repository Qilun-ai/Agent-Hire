"""GitLab's own Nuxt jobs board and its exact Greenhouse detail flow.

Observed journey (see companies/gitlab/research/journey-notes.md):

- Official entry: ``https://about.gitlab.com/jobs/all-jobs/`` (Nuxt SSR).
  Its ``<link rel="preload" href="/jobs/all-jobs/_payload.json?...">``
  exposes the JobsBoard component content: ``jobDivisions`` (6 divisions,
  62 UI departments, 121 ``greenhouseJobBucketIds``) and an
  ``accordion`` Talent Community (6 groups, 19 exact
  ``job-boards.greenhouse.io/gitlabcrm/jobs/<id>`` links).
- JobsBoard's Postings component fetches client-side
  ``https://boards-api.greenhouse.io/v1/boards/gitlab/departments``
  (``useFetch`` with ``server: false``) and renders only board
  departments whose ``id`` is in the static bucket config and whose
  ``jobs`` are non-empty. Location/department dropdowns filter that
  same in-memory dataset; there is no job pagination. Job cards link
  to each row's ``absolute_url`` (``job-boards.greenhouse.io/gitlab/jobs/<id>``).
- Talent Community links are published as exact Greenhouse
  ``gitlabcrm`` job URLs; only those 19 IDs are membership. The
  ``gitlabcrm`` board's other 3 postings are never published on the
  official page and are not collected.
- Full descriptions live on the same boards' per-job endpoints for
  the exact exposed IDs:
  ``boards-api.../gitlab/jobs/<id>`` and
  ``boards-api.../gitlabcrm/jobs/<id>`` (``content`` field, escaped HTML).

The adapter requests only those exact URLs: entry, its advertised
payload, the published departments endpoint (initial + final), the 19
exact talent links' detail endpoints, and each visible posting's
exact detail endpoint. It never enumerates a board.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ENTRY_HOST = "about.gitlab.com"
ENTRY_PATH = "/jobs/all-jobs"
BOARD_API_HOST = "boards-api.greenhouse.io"
MAIN_BOARD = "gitlab"
TALENT_BOARD = "gitlabcrm"
DEPARTMENTS_URL = f"https://{BOARD_API_HOST}/v1/boards/{MAIN_BOARD}/departments"
MAIN_DETAIL_BASE = f"https://{BOARD_API_HOST}/v1/boards/{MAIN_BOARD}/jobs"
TALENT_DETAIL_BASE = f"https://{BOARD_API_HOST}/v1/boards/{TALENT_BOARD}/jobs"
JOB_PAGE_BASE_MAIN = f"https://job-boards.greenhouse.io/{MAIN_BOARD}/jobs"
JOB_PAGE_BASE_TALENT = f"https://job-boards.greenhouse.io/{TALENT_BOARD}/jobs"

PAYLOAD_HREF_RE = re.compile(r'href="(/jobs/all-jobs/_payload\.json\?[^"]+)"')
GH_ID_RE = re.compile(r"^\d{7,13}$")
TALENT_LINK_RE = re.compile(
    r"^https://job-boards\.greenhouse\.io/gitlabcrm/jobs/(\d{7,13})$"
)
FUTURE_PIPELINE_MARKER = "future opportunity that may not be immediately available"


def main_job_url(identity):
    if not GH_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"GitLab main-board job ID is not a Greenhouse ID: {identity!r}")
    return f"{JOB_PAGE_BASE_MAIN}/{identity}"


def talent_job_url(identity):
    if not GH_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"GitLab talent-pool job ID is not a Greenhouse ID: {identity!r}")
    return f"{JOB_PAGE_BASE_TALENT}/{identity}"


def main_detail_url(identity):
    return f"{MAIN_DETAIL_BASE}/{identity}"


def talent_detail_url(identity):
    return f"{TALENT_DETAIL_BASE}/{identity}"


def split_locations(location_name):
    if not isinstance(location_name, str) or not location_name.strip():
        return []
    return [part.strip() for part in location_name.split(";") if part.strip()]


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class GitLab(Adapter):
    """Follow GitLab's official jobs board payload and its bucket filter."""

    VERIFICATION_REVISION = "gitlab.nuxt-payload-bucket-filtered-greenhouse-departments.1"

    # -- Static validators -------------------------------------------------

    @staticmethod
    def _assert_entry_url(url):
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname != ENTRY_HOST
                or parsed.path.rstrip("/") != ENTRY_PATH):
            raise ValueError(f"GitLab dedicated flow must start from its official HTTPS all-jobs entry: {url}")

    @staticmethod
    def _payload_url_from_html(entry_html):
        hrefs = sorted(set(PAYLOAD_HREF_RE.findall(entry_html or "")))
        if len(hrefs) != 1:
            raise ValueError(
                f"GitLab entry does not expose exactly one Nuxt payload link ({len(hrefs)} found)"
            )
        return urljoin(f"https://{ENTRY_HOST}{ENTRY_PATH}/", hrefs[0])

    @staticmethod
    def _resolve_payload_node(payload, node, _depth=0):
        if isinstance(node, int) and 0 <= node < len(payload) and _depth < 24:
            return GitLab._resolve_payload_node(payload, payload[node], _depth + 1)
        if isinstance(node, list):
            return [GitLab._resolve_payload_node(payload, item, _depth + 1) for item in node]
        if isinstance(node, dict):
            return {key: GitLab._resolve_payload_node(payload, value, _depth + 1)
                    for key, value in node.items()}
        return node

    @classmethod
    def _jobs_board_content(cls, payload):
        if not isinstance(payload, list) or not payload:
            raise ValueError("GitLab Nuxt payload is not a JSON array")
        candidates = [node for node in payload
                      if isinstance(node, dict) and "jobDivisions" in node and "accordion" in node]
        if len(candidates) != 1:
            raise ValueError(
                f"GitLab payload does not contain exactly one JobsBoard content node ({len(candidates)})"
            )
        content = cls._resolve_payload_node(payload, candidates[0])
        divisions = content.get("jobDivisions")
        accordion = content.get("accordion")
        if not isinstance(divisions, list) or not divisions:
            raise ValueError("GitLab JobsBoard jobDivisions is missing or empty")
        if not isinstance(accordion, dict) or not isinstance(accordion.get("groups"), list):
            raise ValueError("GitLab JobsBoard talent accordion is missing")
        for division in divisions:
            if not isinstance(division, dict) or not isinstance(division.get("division"), str):
                raise ValueError("GitLab jobDivisions entry lacks a division name")
            if not isinstance(division.get("departments"), list) or not division["departments"]:
                raise ValueError(f"GitLab division {division.get('division')!r} has no departments")
            for dept in division["departments"]:
                if not isinstance(dept, dict) or not isinstance(dept.get("title"), str):
                    raise ValueError("GitLab UI department lacks a title")
                buckets = dept.get("greenhouseJobBucketIds")
                if (not isinstance(buckets, list) or not buckets
                        or any(not isinstance(item, int) or item <= 0 for item in buckets)):
                    raise ValueError(f"GitLab UI department {dept.get('title')!r} has invalid bucket IDs")
        return content

    @staticmethod
    def _talent_entries(content):
        entries = []
        for group in content["accordion"]["groups"]:
            if not isinstance(group, dict) or not isinstance(group.get("title"), str):
                raise ValueError("GitLab talent accordion group lacks a title")
            if not isinstance(group.get("items"), list) or not group["items"]:
                raise ValueError(f"GitLab talent group {group.get('title')!r} has no items")
            for item in group["items"]:
                link = item.get("link") if isinstance(item, dict) else None
                config = link.get("config") if isinstance(link, dict) else None
                href = config.get("href") if isinstance(config, dict) else None
                text = link.get("text") if isinstance(link, dict) else None
                if not isinstance(href, str) or not TALENT_LINK_RE.fullmatch(href):
                    raise ValueError(f"GitLab talent link is not an exact gitlabcrm job URL: {href!r}")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("GitLab talent link lacks card text")
                entries.append({
                    "group": group["title"],
                    "text": text,
                    "href": href,
                    "job_id": TALENT_LINK_RE.fullmatch(href).group(1),
                    "location": item.get("location"),
                })
        ids = [entry["job_id"] for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("GitLab talent accordion repeats a company-published ID")
        return entries

    @staticmethod
    def _departments(payload, label):
        if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
            raise ValueError(f"GitLab {label} departments response has no departments array")
        rows = payload["departments"]
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                raise ValueError(f"GitLab {label} departments contains a missing department ID")
            for job in (row.get("jobs") or []):
                if not isinstance(job, dict) or not isinstance(job.get("id"), int):
                    raise ValueError(f"GitLab {label} department {row.get('id')} has a malformed job row")
                if not job.get("title"):
                    raise ValueError(f"GitLab job {job.get('id')} in {label} listing lacks a title")
        return rows

    @staticmethod
    def _visible_membership(board_rows, content):
        """Apply the Postings bucket filter; return (ordered, provenance)."""
        bucket_to_ui = {}
        for division in content["jobDivisions"]:
            for dept in division["departments"]:
                for bucket_id in dept["greenhouseJobBucketIds"]:
                    bucket_to_ui.setdefault(bucket_id, []).append(
                        (division["division"], dept["title"]))
        board_by_id = {row["id"]: row for row in board_rows}
        missing_buckets = sorted(bucket for bucket in bucket_to_ui if bucket not in board_by_id)
        empty_buckets = sorted(
            bucket for bucket, board_row in board_by_id.items()
            if bucket in bucket_to_ui and not (board_row.get("jobs") or [])
        )
        ordered = []
        occurrences = {}
        for division in content["jobDivisions"]:
            for dept in division["departments"]:
                for bucket_id in dept["greenhouseJobBucketIds"]:
                    board_row = board_by_id.get(bucket_id)
                    if not board_row:
                        continue
                    for job in (board_row.get("jobs") or []):
                        occurrences.setdefault(job["id"], []).append(
                            {"division": division["division"], "department": dept["title"],
                             "bucket_id": bucket_id,
                             "board_department_id": board_row["id"],
                             "board_department_name": board_row.get("name")})
                        ordered.append((job, division["division"], dept["title"],
                                        bucket_id, board_row))
        # Deduplicate to first UI occurrence, preserving order (matches the
        # company's own in-memory render order; duplicates are recorded).
        seen = set()
        unique = []
        for job, division_name, dept_title, bucket_id, board_row in ordered:
            if job["id"] in seen:
                continue
            seen.add(job["id"])
            unique.append((job, division_name, dept_title, bucket_id, board_row))
        excluded = [
            {"department_id": row["id"], "department_name": row.get("name"),
             "jobs": [{"id": job["id"], "title": job.get("title")}
                      for job in (row.get("jobs") or [])]}
            for row in board_rows
            if row["id"] not in bucket_to_ui and (row.get("jobs") or [])
        ]
        provenance = {
            "bucket_ids": sorted(bucket_to_ui),
            "bucket_ui_map": {str(bucket): bucket_to_ui[bucket] for bucket in bucket_to_ui},
            "missing_buckets": missing_buckets,
            "empty_buckets": empty_buckets,
            "duplicate_occurrences": {str(job_id): places
                                      for job_id, places in occurrences.items() if len(places) > 1},
            "excluded_board_departments": excluded,
            "ui_occurrences": len(ordered),
            "unique_visible_jobs": len(unique),
        }
        return unique, provenance

    # -- Main flow ---------------------------------------------------------

    async def _departments_snapshot(self, label):
        response = await self.http.get(DEPARTMENTS_URL)
        if str(response.url) != DEPARTMENTS_URL:
            raise ValueError(f"GitLab {label} departments request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"GitLab {label} departments response is not JSON") from exc
        self._departments(payload, label)
        return payload

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        self._assert_entry_url(entry_url)
        if not self.page_guard():
            return self.result

        entry = await self.http.get(entry_url)
        self._assert_entry_url(str(entry.url))
        if str(entry.url) != entry_url:
            # Entry canonical is the same URL; a redirect elsewhere is a source change.
            raise ValueError(f"GitLab entry redirected away from its official URL: {entry.url}")
        payload_url = self._payload_url_from_html(entry.text)
        payload_response = await self.http.get(payload_url)
        if str(payload_response.url) != payload_url:
            raise ValueError("GitLab Nuxt payload request redirected away from its advertised link")
        try:
            payload = payload_response.json()
        except ValueError as exc:
            raise ValueError("GitLab Nuxt payload response is not JSON") from exc
        content = self._jobs_board_content(payload)
        talent_entries = self._talent_entries(content)

        initial_payload = await self._departments_snapshot("initial")
        initial_rows = initial_payload["departments"]
        initial_unique, initial_provenance = self._visible_membership(initial_rows, content)
        if not initial_unique and not talent_entries:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "GitLab official bucket-filtered listing returned zero visible jobs; empty state requires review"
            )
            return self.result
        if not initial_unique:
            self.result.errors.append("GitLab official board returned zero bucket-visible main jobs")

        # Representative exact details (two exact published IDs, never enumerated):
        # first visible main job + first talent-pool job. Their boards-api
        # bodies are cached and reused by hydration (labelled as reused work).
        prefetched = {}
        representative = {}
        rep_errors = []
        rep_candidates = []
        if initial_unique:
            rep_candidates.append(("main", str(initial_unique[0][0]["id"])))
        if talent_entries:
            rep_candidates.append(("talent", talent_entries[0]["job_id"]))
        rep_started = time.perf_counter()
        listing_by_id_main = {str(job["id"]): job for job, *_ in initial_unique}
        for kind, identity in rep_candidates:
            detail_url = main_detail_url(identity) if kind == "main" else talent_detail_url(identity)
            page_url = main_job_url(identity) if kind == "main" else talent_job_url(identity)
            check = {"detail_url": detail_url, "job_page_url": page_url}
            try:
                response = await self.http.get(detail_url)
                if str(response.url) != detail_url:
                    raise ValueError("representative detail request redirected away from its exact board URL")
                detail = response.json()
                prefetched[identity] = detail
                raw_content = detail.get("content") or ""
                raw_html = html.unescape(raw_content)
                api_text = text_html(raw_html)
                listing_row = listing_by_id_main.get(identity)
                check.update({
                    "title_match": detail.get("title") == (listing_row.get("title") if listing_row else None)
                    if kind == "main" else None,
                    "absolute_url_match": detail.get("absolute_url") == page_url,
                    "company_name": detail.get("company_name"),
                    "content_text_length": len(api_text),
                    "content_compact_sha256": hashlib.sha256(
                        re.sub(r"\s+", "", api_text).encode("utf-8")).hexdigest(),
                    "pipeline_marker_present": FUTURE_PIPELINE_MARKER in api_text.lower(),
                })
                if not isinstance(raw_html, str) or not raw_html.strip():
                    raise ValueError("representative detail content is empty")
                if len(api_text.strip()) < 100:
                    raise ValueError("representative detail description is implausibly short")
                if check.get("absolute_url_match") is not True:
                    raise ValueError("representative detail absolute_url differs from the published link")
                # Independent job-boards page containment (not a rendered browser).
                try:
                    page = await self.http.get(page_url)
                    if str(page.url) != page_url:
                        raise ValueError("representative job page redirected away from its exact URL")
                    soup = BeautifulSoup(page.text, "lxml")
                    job_post = soup.select_one(".job-post")
                    page_text = " ".join(
                        job_post.get_text(" ", strip=True).split()) if job_post else ""
                    # API text must appear inside the published job page text.
                    collapsed_page = " ".join(page_text.split())
                    collapsed_api = " ".join(api_text.split())
                    check["job_page_containment"] = bool(
                        job_post and collapsed_api and collapsed_api in collapsed_page)
                    check["job_page_text_length"] = len(page_text)
                    if not check["job_page_containment"]:
                        rep_errors.append(f"{identity}: boards-api body not contained in job-boards page")
                except Exception as exc:
                    check["job_page_containment"] = None
                    check["job_page_error"] = f"{type(exc).__name__}: {exc}"
                    rep_errors.append(f"{identity}: representative job page {type(exc).__name__}: {exc}")
            except Exception as exc:
                rep_errors.append(f"{identity}: {type(exc).__name__}: {exc}")
                check["error"] = f"{type(exc).__name__}: {exc}"
            representative[identity] = check
        rep_seconds = time.perf_counter() - rep_started
        for item in rep_errors:
            self.result.errors.append(f"GitLab representative exact detail {item}")

        final_snapshot_verified = True
        try:
            final_payload = await self._departments_snapshot("final")
            final_rows = final_payload["departments"]
            final_unique, final_provenance = self._visible_membership(final_rows, content)
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            final_unique, final_provenance = initial_unique, initial_provenance
            self.result.errors.append(
                "GitLab final departments snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        initial_by_id = {(str(job["id"]), "main"): job for job, *_ in initial_unique}
        final_by_id = {(str(job["id"]), "main"): job for job, *_ in final_unique}
        initial_ids = {identity for identity, _ in initial_by_id}
        final_ids = {identity for identity, _ in final_by_id}
        changed_membership = bool(final_snapshot_verified and initial_ids != final_ids)
        if changed_membership:
            self.result.errors.append(
                "gitlab_official_listing_membership_changed_between_initial_and_final_snapshots"
            )

        # Build unified membership: final main rows (preferred) + talent, with
        # initial-only rows retained and flagged (never silently dropped).
        main_plan = []  # (job, division, dept, bucket, board_row, is_stale)
        seen_plan = set()
        for job, div, dept, bucket, brow in final_unique:
            identity = str(job["id"])
            seen_plan.add(identity)
            main_plan.append((job, div, dept, bucket, brow, False))
        stale_main = []
        for job, div, dept, bucket, brow in initial_unique:
            identity = str(job["id"])
            if identity not in seen_plan:
                stale_main.append((job, div, dept, bucket, brow, True))
                seen_plan.add(identity)
        main_plan.extend(stale_main)
        changed_rows = sorted(
            identity for identity in initial_ids & final_ids
            if initial_by_id[(identity, "main")] != final_by_id[(identity, "main")]
        )

        # Create listing jobs (descriptions hydrated below).
        jobs_to_hydrate = []
        for job, div, dept, bucket, brow, is_stale in main_plan:
            identity = str(job["id"])
            jobs_to_hydrate.append(self._listing_job(
                job, division=div, department_title=dept, bucket_id=bucket,
                board_row=brow, board="gitlab", kind="regular_posting",
                raw_detail_url=main_detail_url(identity),
                job_url=main_job_url(identity),
                is_stale=is_stale,
            ))
        talent_seen = set()
        for talent_entry in talent_entries:
            identity = talent_entry["job_id"]
            if identity in talent_seen:
                continue
            talent_seen.add(identity)
            jobs_to_hydrate.append(self._listing_job(
                {"id": int(identity), "title": talent_entry["text"],
                 "absolute_url": talent_entry["href"],
                 "company_name": "GitLab Prospects"},
                division=None, department_title=talent_entry["group"], bucket_id=None,
                board_row=None, board="gitlabcrm", kind="talent_community_pipeline",
                raw_detail_url=talent_detail_url(identity),
                job_url=talent_job_url(identity), is_stale=False,
                talent_entry=talent_entry,
            ))

        async def detail(job):
            job_id = job.job_id
            detail_url = main_detail_url(job_id) if job.raw["source_ui"]["board"] == "gitlab" \
                else talent_detail_url(job_id)
            if job_id in prefetched:
                body = prefetched[job_id]
                job.raw["detail_reused_from_representative_check"] = True
            else:
                response = await self.http.get(detail_url)
                if str(response.url) != detail_url:
                    raise ValueError("detail request redirected away from its exact board URL")
                body = response.json()
            self._apply_detail(job, body, detail_url)
            return job

        hydration_started = time.perf_counter()
        hydrated_jobs = await self.hydrate(jobs_to_hydrate, detail)
        for job in hydrated_jobs:
            self.add(job)
        hydration_seconds = time.perf_counter() - hydration_started

        final_main_ids = [str(job["id"]) for job, *_ in final_unique]
        initial_main_ids = [str(job["id"]) for job, *_ in initial_unique]
        talent_ids = [entry["job_id"] for entry in talent_entries]
        all_collected_ids = [job.job_id for job in jobs_to_hydrate]

        self.result.expected_total = len(final_unique) + len(talent_entries)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "payload_url": payload_url,
            "boards_api_departments_url": DEPARTMENTS_URL,
            "membership_authority": (
                "GitLab's official /jobs/all-jobs page JobsBoard payload: main postings are the "
                "boards-api gitlab/departments rows whose department id is in the static "
                "greenhouseJobBucketIds config with non-empty jobs (client-side location/department "
                "filters over that same dataset); talent-community postings are only the 19 exact "
                "gitlabcrm job URLs published in that same payload. No whole-board enumeration."
            ),
            "ui_pagination": "No job pagination; Postings filters the single departments response in memory by location and division dropdowns.",
            "location_filters": "Client-side only: View all/Runtime locations list over location.name split on ';'.",
            "division_filters": "Client-side only: All Departments plus 6 payload divisions.",
            "initial_visible_main_ids": initial_main_ids,
            "final_visible_main_ids": final_main_ids,
            "talent_community_ids": talent_ids,
            "all_collected_ids_in_ui_order": all_collected_ids,
            "initial_ui_occurrences": initial_provenance["ui_occurrences"],
            "final_ui_occurrences": final_provenance["ui_occurrences"],
            "final_snapshot_verified": final_snapshot_verified,
            "source_rows_changed_between_snapshots": changed_rows,
            "stale_initial_only_main_ids": [str(item[0]["id"]) for item in stale_main],
            "bucket_provenance_final": final_provenance,
            "bucket_provenance_initial": initial_provenance,
            "representative_exact_detail_checks": representative,
            "representative_exact_detail_errors": rep_errors,
            "description_source": (
                "Boards-api per-job `content` (escaped HTML, unescaped once) for each exact "
                "company-published ID; URLs are the row's absolute_url, same as the card link."
            ),
            "salary_policy": "No structured compensation published on these rows (metadata is Quota Coverage Type only); salary stays null.",
            "job_attachment_links": {
                job.job_id: _job_attachment_links(job.description_html or "")
                for job in jobs_to_hydrate if _job_attachment_links(job.description_html or "")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["detail_phase_seconds"] = round(hydration_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(rep_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    def _listing_job(self, listing_row, *, division, department_title, bucket_id,
                     board_row, board, kind, raw_detail_url, job_url, is_stale,
                     talent_entry=None):
        identity = str(listing_row["id"])
        expected_url = main_job_url(identity) if board == "gitlab" else talent_job_url(identity)
        if board == "gitlab" and listing_row.get("absolute_url") not in (expected_url, None):
            raise ValueError(f"GitLab listed ID {identity} has an unexpected absolute_url")
        if talent_entry is not None and talent_entry.get("href") != expected_url:
            raise ValueError(f"GitLab talent ID {identity} link changed from its published URL")
        title = listing_row.get("title") or (talent_entry or {}).get("text") or ""
        if not title:
            raise ValueError(f"GitLab listed ID {identity} lacks a title")
        raw = {
            "listing": listing_row,
            "source_ui": {
                "division": division,
                "department_title": department_title,
                "greenhouse_bucket_id": bucket_id,
                "board_department_id": (board_row or {}).get("id"),
                "board_department_name": (board_row or {}).get("name"),
                "board": board,
                "posting_kind": kind,
                "listed_job_url": listing_row.get("absolute_url") or expected_url,
                "listed_location_name": (listing_row.get("location") or {}).get("name"),
                "detail_api_url": raw_detail_url,
                "talent_entry": talent_entry,
                "attachment_links": [],
            },
        }
        if board_row is not None:
            raw["source_ui"]["board_department_jobs_count"] = len(board_row.get("jobs") or [])
        locations = [{"location": part, "source_location_name": (listing_row.get("location") or {}).get("name")}
                     for part in split_locations((listing_row.get("location") or {}).get("name"))]
        if kind == "talent_community_pipeline" and not locations:
            card_location = (talent_entry or {}).get("location")
            if card_location:
                locations = [{"location": card_location, "source_location_name": card_location}]
        date_posted = listing_row.get("first_published") if isinstance(
            listing_row.get("first_published"), str) else None
        date_updated = listing_row.get("updated_at") if isinstance(
            listing_row.get("updated_at"), str) else None
        job = self.job(
            raw,
            title=title,
            url=expected_url,
            job_id=identity,
            requisition_id=listing_row.get("requisition_id"),
            apply_url=expected_url,
            description="",
            method="gitlab.nuxt_payload_bucket_filter.board_detail_content",
            description_url=raw_detail_url,
            locations=locations,
            department=department_title,
            team=division,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=listing_row.get("first_published"),
            date_updated=date_updated,
            salary=None,
        )
        job.record_identity = f"gitlab:{identity}"
        if is_stale:
            job.warnings.append("membership_changed_during_collection")
        if kind == "talent_community_pipeline":
            job.warnings.append("talent_community_pipeline_expression_of_interest")
        return job

    def _apply_detail(self, job, detail, detail_url):
        if not isinstance(detail, dict):
            raise ValueError("Greenhouse detail response is not an object")
        if str(detail.get("id")) != job.job_id:
            raise ValueError(f"Greenhouse detail ID mismatch for {job.job_id}: {detail.get('id')}")
        company = detail.get("company_name")
        board = job.raw["source_ui"]["board"]
        expected_employer = "GitLab Prospects" if board == "gitlabcrm" else "GitLab"
        if company is not None and company != expected_employer:
            raise ValueError(
                f"Greenhouse detail for {job.job_id} has unexpected employer: {company!r} "
                f"(expected {expected_employer!r} for board {board})")
        title = detail.get("title")
        if isinstance(title, str) and title.strip():
            job.title = title
        expected_page = main_job_url(job.job_id) if board == "gitlab" else talent_job_url(job.job_id)
        if detail.get("absolute_url") != expected_page:
            raise ValueError(f"Greenhouse detail for {job.job_id} absolute_url differs from published link")
        raw_content = detail.get("content") or ""
        if not isinstance(raw_content, str) or not raw_content.strip():
            self.result.errors.append(f"{job.job_id}: GitLab Greenhouse detail content is empty")
            return
        description_html = html.unescape(raw_content)
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            self.result.errors.append(f"{job.job_id}: GitLab Greenhouse detail description is implausibly short")
        job.raw["detail"] = detail
        job.description_html = description_html
        job.description_text = description_text
        # Prefer detail dates/IDs as source of truth when present.
        if isinstance(detail.get("first_published"), str):
            job.date_posted = detail["first_published"]
            job.date_posted_raw = detail["first_published"]
        if isinstance(detail.get("updated_at"), str):
            job.date_updated = detail["updated_at"]
        if detail.get("requisition_id") is not None:
            job.requisition_id = detail.get("requisition_id")
        deadline = detail.get("application_deadline")
        if isinstance(deadline, str) and deadline.strip():
            job.valid_through = deadline
        detail_location = (detail.get("location") or {}).get("name")
        if detail_location:
            job.locations = [{"location": part, "source_location_name": detail_location}
                             for part in split_locations(detail_location)]
        dept_names = [d.get("name") for d in (detail.get("departments") or [])
                      if isinstance(d, dict) and d.get("name")]
        if dept_names:
            job.raw["source_ui"]["board_detail_departments"] = dept_names
            job.raw["source_ui"]["detail_location_name"] = detail_location
        job.raw["source_ui"]["attachment_links"] = _job_attachment_links(description_html)
        job.raw["source_ui"]["is_future_pipeline"] = (
            FUTURE_PIPELINE_MARKER in description_text.lower())
        if job.raw["source_ui"]["attachment_links"]:
            job.warnings.append("role_attachment_links_present_without_full_attachment_text")
        # Description provenance stays on the exact detail endpoint.
        job.description_method = "gitlab.nuxt_payload_bucket_filter.board_detail_content"
        job.description_source_url = detail_url
        job.employer = detail.get("company_name")
        job.language = detail.get("language")
