"""Discord's own Webflow careers listing and React job-detail journey.

Membership authority (inspected 2026-11-07):

- Official entry ``https://discord.com/careers`` publishes one Webflow
  script ``/webflow-scripts/careers<digits>.js``. That script defines
  ``DISCORD_JOB_BOARDS=["discord","discordinternational","internationaleor"]``
  and fetches ``https://api.greenhouse.io/v1/boards/<board>/jobs?content=true``
  for each board (``Promise.allSettled``). It renders *all* combined rows
  grouped by display department (team), sorted by department name then title,
  and only applies department/location filters client-side. There is no
  job pagination: the ``Load More`` button is shipped hidden and the script
  never slices or pages the combined list.
- Each card links to Discord's native ``/jobs/<greenhouse-id>`` route
  (``target="_blank"``). That React detail page fetches the same three
  ``?content=true`` boards, selects the row by its URL ``jobId``, renders
  that row's full ``content`` and points its Apply CTA at
  ``absolute_url + "?gh_src=57e244b12us"``.
- No universal ATS discovery is used: only the three exact board URLs the
  company scripts publish are requested, and only those exact IDs are kept.
"""
import html as _html
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


EXPECTED_BOARDS = ["discord", "discordinternational", "internationaleor"]
API_BASE = "https://api.greenhouse.io/v1/boards/"
API_SUFFIX = "/jobs?content=true"
LISTING_GH_SRC = "DiscordJobs"
DETAIL_GH_SRC = "57e244b12us"
GH_HOST = "job-boards.greenhouse.io"
CAREERS_SCRIPT_RE = re.compile(r"^/webflow-scripts/careers\d+\.js$")
BOARDS_RE = re.compile(r"DISCORD_JOB_BOARDS\s*=\s*\[([^\]]+)\]")
FETCH_TEMPLATE = "https://api.greenhouse.io/v1/boards/"
DISPLAY_OVERRIDE_ID = 96196709002
PRIORITY_HIRE_ID = 4105571002
SF_OFFICE_ID = 4000383002


def compact_body_hash(decoded_html):
    """NFC/whitespace-free hash of the rendered description text."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(decoded_html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(entry_url, identity):
    identity = str(identity)
    if not re.fullmatch(r"\d{7,}", identity):
        raise ValueError(f"Discord official listing job ID is not numeric: {identity!r}")
    return urljoin(entry_url, f"/jobs/{identity}")


def board_api_url(board):
    if board not in EXPECTED_BOARDS:
        raise ValueError(f"Discord job board is not one of the inspected company boards: {board!r}")
    return f"{API_BASE}{board}{API_SUFFIX}"


def display_team(row):
    """Apply the Careers Site Department metadata override, as the company UI does."""
    for meta in row.get("metadata") or []:
        if meta.get("id") == DISPLAY_OVERRIDE_ID:
            value = meta.get("value")
            if value is not None and not isinstance(value, list) and str(value).strip():
                return str(value)
            break
    departments = row.get("departments") or []
    if not departments or not departments[0].get("name"):
        raise ValueError("Discord listing row has no department name or override")
    return str(departments[0]["name"])


def is_priority_hire(row):
    for meta in row.get("metadata") or []:
        if meta.get("id") == PRIORITY_HIRE_ID:
            value = meta.get("value") or []
            if isinstance(value, list):
                return any(str(item) == "1" for item in value)
            return str(value) == "1"
    return False


def _salary_from_content(decoded_html):
    soup = BeautifulSoup(decoded_html or "", "lxml")
    node = soup.select_one(".pay-range")
    if not node:
        return None
    spans = [span.get_text(strip=True) for span in node.select("span")]
    spans = [span for span in spans if span and span != "—"]
    if len(spans) < 2:
        return None
    return {
        "source_text": f"{spans[0]} — {spans[1]}",
        "source_field": "content.pay-range",
        "semantics": (
            "Base salary range as published in the role's pay-transparency block; "
            "source states compensation details reflect base salary only and exclude equity/benefits."
        ),
    }


def _attachment_links(decoded_html):
    soup = BeautifulSoup(decoded_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _decode_content(row):
    raw_content = row.get("content")
    if raw_content is None:
        raise ValueError(f"Discord job {row.get('id')} has no Greenhouse content field")
    # Greenhouse serves the description HTML escaped (e.g. "&lt;div..."); the
    # company detail component runs lodash-style unescape on it before render.
    return _html.unescape(str(raw_content))


class Discord(Adapter):
    """Follow only the scripts and board URLs Discord itself publishes."""

    VERIFICATION_REVISION = "discord.official-careers-three-board-list-and-native-detail.1"
    CAREERS_HOST = "discord.com"
    DETAIL_HOSTS = {"discord.com", "www.discord.com"}

    @staticmethod
    def _assert_discord_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Discord.DETAIL_HOSTS:
            raise ValueError(f"Discord company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Discord company page asset has an unexpected path: {url}")

    @staticmethod
    def _careers_script_url(entry_html):
        soup = BeautifulSoup(entry_html, "lxml")
        candidates = [node.get("src") for node in soup.select("script[src]") if node.get("src")]
        matches = [src for src in candidates if CAREERS_SCRIPT_RE.fullmatch(urlsplit(src).path)]
        if len(matches) != 1:
            raise ValueError(
                f"Discord official /careers page has {len(matches)} careers scripts; expected exactly one"
            )
        return matches[0]

    @staticmethod
    def _boards_from_script(script):
        match = BOARDS_RE.search(script or "")
        if not match:
            raise ValueError("Discord careers script no longer publishes DISCORD_JOB_BOARDS")
        boards = [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()]
        if boards != EXPECTED_BOARDS:
            raise ValueError(f"Discord careers script boards changed from their inspected list: {boards!r}")
        if FETCH_TEMPLATE not in script or API_SUFFIX not in script:
            raise ValueError("Discord careers script no longer fetches the inspected Greenhouse board template")
        return boards

    @staticmethod
    def _assert_listing_script(script):
        required = (
            "DISCORD_JOB_BOARDS", "content=true", "absolute_url", "gh_src=DiscordJobs",
            "/jobs/${", "data-name", "data-location", "tag-future",
        )
        if any(token not in script for token in required):
            raise ValueError("Discord careers script no longer matches the inspected list/filter/card flow")

    @staticmethod
    def _rows(payload, board, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"Discord {snapshot_name} listing for board {board!r} has no jobs array")
        rows = payload["jobs"]
        ids = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Discord {snapshot_name} listing for board {board!r} contains a malformed row")
            identity = row.get("id")
            if not isinstance(identity, int) or not row.get("title"):
                raise ValueError(
                    f"Discord {snapshot_name} listing for board {board!r} contains a missing/bad ID or title"
                )
            expected = f"https://{GH_HOST}/{board}/jobs/{identity}"
            if row.get("absolute_url") != expected:
                raise ValueError(
                    f"Discord job {identity} on board {board!r} has unexpected absolute_url "
                    f"{row.get('absolute_url')!r}"
                )
            ids.append(identity)
        if len(ids) != len(set(ids)):
            raise ValueError(f"Discord {snapshot_name} listing for board {board!r} repeats an ID")
        return rows

    @staticmethod
    def ui_order(rows):
        """Group by display department, sort departments and titles, as the company UI does."""
        groups = {}
        for row in rows:
            groups.setdefault(display_team(row), []).append(row)
        ordered = []
        for department in sorted(groups):
            ordered.extend(sorted(groups[department], key=lambda row: row["title"]))
        return ordered

    def _make_job(self, row, *, board, entry_url, api_url, detail_chunk_url,
                  initial_row=None, membership_changed=False):
        identity = str(row["id"])
        description_html = _decode_content(row)
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Discord public board content decodes to empty HTML")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Discord public board description is implausibly short")
        salary = _salary_from_content(description_html)
        native_url = native_job_url(entry_url, identity)
        greenhouse_url = row["absolute_url"]
        source_ui = {
            "native_card_url": native_url,
            "greenhouse_absolute_url": greenhouse_url,
            "rendered_apply_url": greenhouse_url + "?gh_src=" + DETAIL_GH_SRC,
            "listing_script_link": greenhouse_url + "?gh_src=" + LISTING_GH_SRC,
            "display_department": display_team(row),
            "greenhouse_department": (row.get("departments") or [{}])[0].get("name"),
            "job_board": board,
            "listing_api_url": api_url,
            "detail_chunk_url": detail_chunk_url,
            "is_priority_hire": is_priority_hire(row),
            "hide_perks_and_benefits": "Contract" in (row.get("title") or ""),
            "is_cover_letter_required": any(
                dept.get("id") != 4000657002 for dept in (row.get("departments") or [])
            ),
            "updated_at": row.get("updated_at"),
            "first_published": row.get("first_published"),
            "attachment_links": _attachment_links(description_html),
            "salary_from_content": salary,
        }
        raw = {"listing": row, "source_ui": source_ui}
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=native_url,
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=source_ui["rendered_apply_url"],
            description=description_html,
            method="discord.official_careers_embed.greenhouse_board_content",
            description_url=api_url,
            locations=[{"location": (row.get("location") or {}).get("name"),
                        "offices": row.get("offices") or []}],
            department=source_ui["display_department"],
            employment_type=None,
            workplace_type=None,
            date_posted=row.get("first_published"),
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            salary=salary,
            employer=row.get("company_name"),
            language=row.get("language"),
        )
        job.description_text = description_text
        job.record_identity = f"discord:{board}:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def _board_snapshot(self, board, label):
        api_url = board_api_url(board)
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Discord {label} listing for board {board!r} redirected to {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Discord {label} listing for board {board!r} is not JSON") from exc
        self._rows(payload, board, label)
        return payload

    async def _detail_chunk_url(self, detail_html, detail_page_url):
        soup = BeautifulSoup(detail_html, "lxml")
        scripts = [node.get("src") for node in soup.select("script[data-chunk][src]")
                   if node.get("data-chunk") == "refresh-jobs-Job" and node.get("src")]
        if not scripts:
            raise ValueError("Discord exact native detail page exposes no refresh-jobs-Job chunk scripts")
        found = []
        for src in scripts:
            chunk_url = urljoin(detail_page_url, src)
            self._assert_discord_url(chunk_url, path_prefix="/assets/")
            response = await self.http.get(chunk_url)
            if FETCH_TEMPLATE in response.text and '["discord","discordinternational","internationaleor"]' in response.text:
                found.append(str(response.url))
        if len(found) != 1:
            raise ValueError(f"Discord native detail chunk board source is missing/ambiguous ({len(found)})")
        return found[0]

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"discord.com", "www.discord.com"}
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("Discord dedicated flow must start from its official HTTPS /careers entry")
        entry = await self.http.get(entry_url)
        self._assert_discord_url(entry.url, path_prefix="/careers")
        listing_script_url = self._careers_script_url(entry.text)
        listing_script_url = urljoin(str(entry.url), listing_script_url)
        self._assert_discord_url(listing_script_url, path_prefix="/webflow-scripts/")
        listing_response = await self.http.get(listing_script_url)
        self._assert_discord_url(listing_response.url, path_prefix="/webflow-scripts/")
        listing_script_url = str(listing_response.url)
        listing_script = listing_response.text
        boards = self._boards_from_script(listing_script)
        self._assert_listing_script(listing_script)

        initial_rows = []
        initial_by_board = {}
        initial_payloads = {}
        for board in boards:
            payload = await self._board_snapshot(board, "initial")
            initial_payloads[board] = payload
            initial_by_board[board] = {row["id"]: row for row in payload["jobs"]}
            initial_rows.extend(payload["jobs"])
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Discord official embedded listings returned zero visible jobs; native empty state requires independent review"
            )
            return self.result

        first_row = self.ui_order(initial_rows)[0]
        # The first UI-ordered row's board is needed for its exact native detail route.
        first_board = next(board for board in boards if first_row["id"] in initial_by_board[board])
        exact_detail_page_url = native_job_url(str(entry.url), first_row["id"])
        detail_started = time.perf_counter()
        detail_chunk_url = None
        detail_ui_error = None
        try:
            detail_page = await self.http.get(exact_detail_page_url)
            self._assert_discord_url(detail_page.url, path_prefix="/jobs/")
            if urlsplit(str(detail_page.url)).path != f"/jobs/{first_row['id']}":
                raise ValueError("Discord exact native detail route redirected to a different job ID or page")
            soup = BeautifulSoup(detail_page.text, "lxml")
            og_title = (soup.select_one('meta[property="og:title"]') or {}).get("content")
            if og_title != f"Discord — {first_row['title']}":
                raise ValueError(
                    f"Discord exact native detail og:title {og_title!r} does not match {first_row['title']!r}"
                )
            og_description = (soup.select_one('meta[property="og:description"]') or {}).get("content") or ""
            expected_text = text_html(_decode_content(first_row))
            normalized_og = re.sub(r"\s+", " ", _html.unescape(og_description)).strip()
            normalized_expected = re.sub(r"\s+", " ", expected_text).strip()
            if normalized_og[:200] != normalized_expected[:200]:
                raise ValueError("Discord exact native detail og:description does not start with the board content")
            detail_chunk_url = await self._detail_chunk_url(detail_page.text, str(detail_page.url))
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Discord representative exact detail UI {exact_detail_page_url}: {detail_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        final_payloads = {}
        final_rows = []
        final_by_board = {}
        try:
            for board in boards:
                payload = await self._board_snapshot(board, "final")
                final_payloads[board] = payload
                final_by_board[board] = {row["id"]: row for row in payload["jobs"]}
                final_rows.extend(payload["jobs"])
        except Exception as exc:
            final_snapshot_verified = False
            final_payloads = initial_payloads
            final_rows = initial_rows
            final_by_board = initial_by_board
            self.result.errors.append(
                "Discord final company-published listing snapshots failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )

        initial_ids = {(board, identity) for board, rows in initial_by_board.items() for identity in rows}
        final_ids = {(board, identity) for board, rows in final_by_board.items() for identity in rows}
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("discord_official_listing_membership_changed_between_initial_and_final_snapshots")

        ui_final_rows = self.ui_order(final_rows)
        ui_initial_rows = self.ui_order(initial_rows)

        def board_of(row):
            for board in boards:
                if row["id"] in initial_by_board.get(board, {}) or row["id"] in final_by_board.get(board, {}):
                    if row["id"] in {**initial_by_board.get(board, {}), **final_by_board.get(board, {})}:
                        return board
            return boards[0]

        all_keys = list(dict.fromkeys(
            [(board_of(row), row["id"]) for row in ui_final_rows]
            + [(board_of(row), row["id"]) for row in ui_initial_rows]
        ))
        changed_rows = sorted({
            identity for board in boards
            for identity in initial_by_board.get(board, {}).keys() & final_by_board.get(board, {}).keys()
            if initial_by_board[board][identity] != final_by_board[board][identity]
        })
        mapping_started = time.perf_counter()
        for board, identity in all_keys:
            final_row = final_by_board.get(board, {}).get(identity)
            initial_row = initial_by_board.get(board, {}).get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, board=board, entry_url=str(entry.url), api_url=board_api_url(board),
                detail_chunk_url=detail_chunk_url,
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_board.get(board, {}),
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        ui_initial_ids = [str(row["id"]) for row in ui_initial_rows]
        ui_final_ids = [str(row["id"]) for row in ui_final_rows]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_script_url": listing_script_url,
            "job_boards": boards,
            "listing_api_urls": {board: board_api_url(board) for board in boards},
            "membership_authority": (
                "Discord /careers Webflow script's unfiltered combined Greenhouse board responses "
                "(discord, discordinternational, internationaleor); department/location controls are client-side only"
            ),
            "listing_filter_behavior": (
                "All combined board rows are rendered grouped by display department, sorted by department "
                "then title; UI filters by display department (data-name) and office names (data-location) only"
            ),
            "ui_pagination": "No job pagination; the shipped Load More button is hidden and the script renders every combined row",
            "department_filters": sorted({display_team(row) for row in final_rows}),
            "location_filters": sorted({
                office.get("name") for row in final_rows
                for office in (row.get("offices") or []) if office.get("name")
            }),
            "initial_api_rows": {board: len(payload["jobs"]) for board, payload in initial_payloads.items()},
            "final_api_rows": {board: len(payload["jobs"]) for board, payload in final_payloads.items()},
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": ui_initial_ids,
            "final_visible_ids": ui_final_ids,
            "source_rows_changed_between_snapshots": changed_rows,
            "exact_native_detail_page_opened": exact_detail_page_url,
            "exact_native_detail_job_id": str(first_row["id"]),
            "exact_native_detail_job_board": first_board,
            "representative_detail_component_reads_id_and_same_boards": detail_ui_error is None,
            "representative_detail_ui_error": detail_ui_error,
            "detail_chunk_url": detail_chunk_url,
            "description_source": (
                "Each exact listed row's Greenhouse content (HTML-escaped) decoded once; the native "
                "/jobs/<id> detail component fetches the same three ?content=true boards and selects by URL jobId"
            ),
            "salary_policy": (
                "Exact Base Salary pay-range text from the published content block is retained with its "
                "source field and base-salary-only semantics; roles without a pay block keep null salary."
            ),
            "job_attachment_links": {
                str(row["id"]): _attachment_links(_decode_content(row))
                for row in final_rows if _attachment_links(_decode_content(row))
            },
            "display_department_overrides": sorted({
                str(row["id"]) for row in final_rows
                if display_team(row) != ((row.get("departments") or [{}])[0].get("name"))
            }),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests_per_board": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
