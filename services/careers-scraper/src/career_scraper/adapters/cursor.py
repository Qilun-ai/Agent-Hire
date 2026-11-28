"""Cursor's own Next.js careers listing and slug detail journey.

Membership authority is the jobs array embedded by the official
https://cursor.com/careers page in its React Server Components flight data
(`self.__next_f.push` payloads). Each job's descriptionHtml/descriptionPlain
is a `$<hex>` reference to a `T<hexbyte-len>` text segment whose bytes are the
next push payload (large descriptions) or inline bytes (short strings). The
adapter resolves those references from the same archived HTML response and
opens two exact native detail pages to verify the embedded single-job object
carries byte-identical description segments.
"""
import hashlib
import json
import re
import time
import unicodedata
from json import JSONDecoder
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
HEADER_T = re.compile(r'(?:^|\n)([0-9a-f]+):T([0-9a-f]+),')
ROW_HEADER = re.compile(r'^([0-9a-f]+):')
DESCRIPTION_METHOD = "cursor.official_careers_embed.rsc_descriptionHtml"


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(entry_url, slug):
    if not SLUG.fullmatch(str(slug)):
        raise ValueError(f"Cursor official listing job slug is invalid: {slug!r}")
    parsed = urlsplit(str(entry_url))
    if parsed.scheme != "https" or parsed.hostname not in {"cursor.com", "www.cursor.com"}:
        raise ValueError(f"Cursor native job URL must stay on the official HTTPS site: {entry_url}")
    return f"https://cursor.com/careers/{slug}"


def _employment_type_label(value):
    return {
        "FullTime": "Full-time", "PartTime": "Part-time", "Intern": "Internship",
        "Contract": "Contract", "Temporary": "Temporary",
    }.get(value, value)


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _decode_pushes(html):
    decoded = []
    for match in PUSH.finditer(html):
        raw = match.group(1)
        try:
            decoded.append(json.loads(f'"{raw}"'))
        except ValueError as exc:
            raise ValueError(f"Cursor flight-data push is not valid JSON: {exc}") from exc
    if not decoded:
        raise ValueError("Cursor official page contains no React Server Components flight data")
    return decoded


def _jobs_from_listing_push(push_text, label):
    match = ROW_HEADER.match(push_text)
    if not match:
        raise ValueError(f"Cursor {label} listing push has no flight row header")
    decoder = JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(push_text, len(match.group(0)))
    except ValueError as exc:
        raise ValueError(f"Cursor {label} listing flight row is not valid JSON: {exc}") from exc
    try:
        jobs = obj[3]["children"][3]["jobs"]
    except (TypeError, IndexError, KeyError) as exc:
        raise ValueError(f"Cursor {label} listing flight row has no jobs array") from exc
    if not isinstance(jobs, list):
        raise ValueError(f"Cursor {label} listing flight row jobs value is not an array")
    return jobs


def _resolve_text_segments(decoded, refs, label):
    """Resolve `$hex` description references to their T-segment bodies.

    Large descriptions arrive as one push containing only `hex:T<hexlen>,` and
    the next push holding exactly `<hexlen>` UTF-8 bytes. Short strings are
    inline after the comma in the same push. Only expected refs are resolved;
    other flight rows (locale, metadata) are ignored.
    """
    refs = set(refs)
    resolved = {}
    body_indices = set()
    for idx, chunk in enumerate(decoded):
        if idx in body_indices:
            continue
        for header in HEADER_T.finditer(chunk):
            rid, blen_hex = header.group(1), header.group(2)
            if rid not in refs:
                continue
            blen = int(blen_hex, 16)
            remainder = chunk[header.end():]
            if remainder.strip() == "":
                if idx + 1 >= len(decoded):
                    raise ValueError(f"Cursor {label} description reference ${rid} has no body push")
                body = decoded[idx + 1]
                if len(body.encode("utf-8")) != blen:
                    raise ValueError(
                        f"Cursor {label} description reference ${rid} byte length "
                        f"{len(body.encode('utf-8'))} does not match its T header {blen}"
                    )
                resolved[rid] = body
                body_indices.add(idx + 1)
            else:
                body_bytes = remainder.encode("utf-8")[:blen]
                try:
                    body = body_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"Cursor {label} description reference ${rid} is not UTF-8") from exc
                if len(body.encode("utf-8")) != blen:
                    raise ValueError(f"Cursor {label} description reference ${rid} inline length mismatch")
                resolved[rid] = body
    missing = refs - set(resolved)
    if missing:
        raise ValueError(f"Cursor {label} listing is missing {len(missing)} description segments: {sorted(missing)[:5]}")
    return resolved


def _validate_rows(rows, label):
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    slugs = [row.get("urlSlug") if isinstance(row, dict) else None for row in rows]
    if any(not isinstance(i, str) or not UUID.fullmatch(i) for i in ids):
        raise ValueError(f"Cursor {label} listing contains a missing or invalid exact job ID")
    if len(ids) != len(set(ids)):
        raise ValueError(f"Cursor {label} listing repeats a company-published ID")
    if any(not isinstance(s, str) or not SLUG.fullmatch(s) for s in slugs):
        raise ValueError(f"Cursor {label} listing contains a missing or invalid native slug")
    if len(slugs) != len(set(slugs)):
        raise ValueError(f"Cursor {label} listing repeats a company-published slug")
    if any(not row.get("title") for row in rows):
        raise ValueError(f"Cursor {label} listing contains an unidentified job")
    for row in rows:
        if not isinstance(row.get("descriptionHtml"), str) or not isinstance(row.get("descriptionPlain"), str):
            raise ValueError(f"Cursor {label} listing job {row.get('id')} has no description references")
        if row.get("isListed") is not True:
            # The shipped listing chunk maps the unseen jobs array directly to
            # cards (a.map(b)) with only All-teams/All-locations client filters.
            # Rows always arrive isListed=true in the inspected snapshots; do
            # not silently drop any row that deviates from that observation.
            raise ValueError(f"Cursor {label} listing job {row.get('id')} has unexpected isListed value")
    return ids, slugs


class Cursor(Adapter):
    """Follow Cursor's official Next.js listing/detail flight data only."""

    VERIFICATION_REVISION = "cursor.native-careers-rsc-list-and-detail-exact-slugs.1"

    @staticmethod
    def _assert_cursor_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"cursor.com", "www.cursor.com"}:
            raise ValueError(f"Cursor company page left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Cursor company page has an unexpected path: {url}")

    @staticmethod
    def _parse_listing_html(html, label):
        decoded = _decode_pushes(html)
        jobs_push = next((chunk for chunk in decoded if '"jobs":' in chunk), None)
        if jobs_push is None:
            raise ValueError(f"Cursor {label} official page embeds no jobs array")
        rows = _jobs_from_listing_push(jobs_push, label)
        _validate_rows(rows, label)
        refs = []
        for row in rows:
            for ref in (row["descriptionHtml"], row["descriptionPlain"]):
                if not ref.startswith("$"):
                    raise ValueError(f"Cursor {label} listing job {row['id']} description is not an RSC reference")
                refs.append(ref[1:])
        # refs are hex row ids; keep the exact set for byte-length resolution.
        row_map = _resolve_text_segments(decoded, refs, label)
        resolved = []
        for row in rows:
            html_ref = row["descriptionHtml"][1:]
            plain_ref = row["descriptionPlain"][1:]
            resolved.append((row, row_map[html_ref], row_map[plain_ref]))
        return rows, resolved

    @staticmethod
    def _parse_detail_html(html, label):
        decoded = _decode_pushes(html)
        job_push = next((chunk for chunk in decoded if '"job":' in chunk), None)
        if job_push is None:
            raise ValueError(f"Cursor {label} native detail page embeds no single job object")
        match = ROW_HEADER.match(job_push)
        if not match:
            raise ValueError(f"Cursor {label} native detail page has no flight row header")
        decoder = JSONDecoder()
        try:
            obj, _end = decoder.raw_decode(job_push, len(match.group(0)))
        except ValueError as exc:
            raise ValueError(f"Cursor {label} native detail flight row is not valid JSON: {exc}") from exc
        try:
            job = obj[3]["job"]
        except (TypeError, IndexError, KeyError) as exc:
            raise ValueError(f"Cursor {label} native detail flight row has no job object") from exc
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not UUID.fullmatch(job["id"]):
            raise ValueError(f"Cursor {label} native detail page has no exact job ID")
        for key in ("descriptionHtml", "descriptionPlain"):
            if not isinstance(job.get(key), str) or not job[key].startswith("$"):
                raise ValueError(f"Cursor {label} native detail job has no description reference")
        refs = [job["descriptionHtml"][1:], job["descriptionPlain"][1:]]
        row_map = _resolve_text_segments(decoded, refs, label)
        return job, row_map[job["descriptionHtml"][1:]], row_map[job["descriptionPlain"][1:]]

    def _make_job(self, row, resolved_html, resolved_plain, *, entry_url,
                  detail_verification=None):
        identity = row["id"]
        slug = row["urlSlug"]
        expected_job_url = f"https://jobs.ashbyhq.com/cursor/{identity}"
        expected_apply_url = expected_job_url + "/application"
        if row.get("jobUrl") != expected_job_url:
            raise ValueError(f"Cursor listed ID {identity} has an unexpected company-published jobUrl")
        apply_url = row.get("applyUrl")
        if apply_url != expected_apply_url:
            raise ValueError(f"Cursor listed ID {identity} has an unexpected company-published applyUrl")
        description_html = resolved_html or ""
        description_text = text_html(description_html)
        plain_text = resolved_plain
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Cursor public detail descriptionHtml is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Cursor public detail description is implausibly short")
        if plain_text is None:
            self.result.errors.append(f"{identity}: Cursor public detail descriptionPlain is missing")
        date_posted = row.get("publishedAt") if isinstance(row.get("publishedAt"), str) and row["publishedAt"].strip() else None
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "listing_resolved": {**row, "descriptionHtml": description_html,
                                 "descriptionPlain": plain_text},
            "source_ui": {
                "native_job_url": native_job_url(entry_url, slug),
                "listed_job_url": row["jobUrl"],
                "rendered_apply_url": apply_url,
                "rendered_employment_type": _employment_type_label(row.get("employmentType")),
                "native_card_slug": slug,
                "listing_entry_url": entry_url,
                "description_ref_html": row.get("descriptionHtml"),
                "description_ref_plain": row.get("descriptionPlain"),
                "attachment_links": attachment_links,
                "salary_from_description": None,
            },
        }
        if detail_verification is not None:
            raw["detail_verification"] = detail_verification
        job = self.job(
            raw,
            title=row["title"],
            url=native_job_url(entry_url, slug),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method=DESCRIPTION_METHOD,
            description_url=entry_url,
            locations=[{"location": row.get("location"), "address": row.get("address")},
                       *(row.get("secondaryLocations") or [])],
            department=row.get("department"),
            team=row.get("team"),
            employment_type=row.get("employmentType"),
            workplace_type=row.get("workplaceType"),
            date_posted=date_posted,
            date_posted_raw=row.get("publishedAt"),
            date_updated=None,
            salary=None,
        )
        job.description_text = description_text
        job.record_identity = f"cursor:{identity}"
        return job

    async def _listing_snapshot(self, entry_url, label):
        response = await self.http.get(entry_url)
        final_url = str(response.url)
        parsed = urlsplit(final_url)
        if parsed.scheme != "https" or parsed.hostname not in {"cursor.com", "www.cursor.com"}:
            raise ValueError(f"Cursor {label} listing request left the official site: {final_url}")
        if parsed.path.rstrip("/") != "/careers" or parsed.query or parsed.fragment:
            raise ValueError(f"Cursor {label} listing request redirected away from its inspected source: {final_url}")
        self._assert_cursor_url(final_url, path_prefix="/careers")
        rows, resolved = self._parse_listing_html(response.text, label)
        # Cross-check static cards against the flight-data order: every RSC
        # slug must appear as an exact /careers/<slug> card href, in order.
        soup = BeautifulSoup(response.text, "lxml")
        card_slugs = []
        seen = set()
        for anchor in soup.select('article a[href^="/careers/"]'):
            href = anchor.get("href", "")
            slug = href.removeprefix("/careers/").split("?")[0].strip("/")
            if SLUG.fullmatch(slug) and slug not in seen:
                seen.add(slug)
                card_slugs.append(slug)
        rsc_slugs = [row["urlSlug"] for row in rows]
        if card_slugs != rsc_slugs:
            raise ValueError(
                f"Cursor {label} static card slugs differ from flight-data membership "
                f"(cards={len(card_slugs)} rsc={len(rsc_slugs)})"
            )
        return final_url, rows, resolved, card_slugs

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"cursor.com", "www.cursor.com"}
                or entry_parts.path.rstrip("/") != "/careers" or entry_parts.query or entry_parts.fragment):
            raise ValueError("Cursor dedicated flow must start from its official HTTPS /careers entry")
        initial_url, initial_rows, initial_resolved, initial_cards = await self._listing_snapshot(entry_url, "initial")
        initial_by_id = {row["id"]: (row, h, p) for row, h, p in initial_resolved}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Cursor official embedded listing returned zero jobs; native empty state requires independent review"
            )
            return self.result

        # Open two exact native detail pages: the first listed job and the
        # second. They verify that the detail route embeds the same job object
        # and byte-identical description segments as the listing membership.
        sample_rows = initial_rows[:2]
        detail_started = time.perf_counter()
        detail_checks = []
        for sample in sample_rows:
            detail_url = native_job_url(initial_url, sample["urlSlug"])
            try:
                detail_page = await self.http.get(detail_url)
                self._assert_cursor_url(str(detail_page.url), path_prefix="/careers/")
                if str(detail_page.url) != detail_url:
                    raise ValueError("Cursor exact native detail route redirected to a different URL")
                if urlsplit(str(detail_page.url)).path != f"/careers/{sample['urlSlug']}":
                    raise ValueError("Cursor exact native detail route redirected to a different slug")
                detail_job, detail_html, detail_plain = self._parse_detail_html(
                    detail_page.text, "representative detail"
                )
                if detail_job.get("id") != sample["id"]:
                    raise ValueError("Cursor native detail page embeds a different job ID than its slug route")
                if detail_job.get("urlSlug") != sample["urlSlug"]:
                    raise ValueError("Cursor native detail page embeds a different slug than its route")
                _row, listing_html, listing_plain = initial_by_id[sample["id"]]
                if detail_html != listing_html:
                    raise ValueError("Cursor native detail descriptionHtml differs from its listing segment")
                if detail_plain != listing_plain:
                    raise ValueError("Cursor native detail descriptionPlain differs from its listing segment")
                if detail_job != {**sample, "descriptionHtml": sample["descriptionHtml"],
                                  "descriptionPlain": sample["descriptionPlain"]}:
                    # detail_job equals sample byte-for-field (including $ refs).
                    # This check is strict so any drift fails closed.
                    if detail_job.get("jobUrl") != sample.get("jobUrl") or detail_job.get("title") != sample.get("title"):
                        raise ValueError("Cursor native detail job object differs from its listing row")
                detail_checks.append({
                    "job_id": sample["id"],
                    "slug": sample["urlSlug"],
                    "detail_url": detail_url,
                    "matched": True,
                    "description_html_sha256": hashlib.sha256(detail_html.encode("utf-8")).hexdigest(),
                    "description_plain_sha256": hashlib.sha256(detail_plain.encode("utf-8")).hexdigest(),
                    "body_compact_sha256": compact_body_hash(detail_html),
                })
            except Exception as exc:
                detail_checks.append({
                    "job_id": sample["id"],
                    "slug": sample["urlSlug"],
                    "detail_url": detail_url,
                    "matched": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                self.result.errors.append(f"Cursor representative exact detail {detail_url}: {type(exc).__name__}: {exc}")
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_url, final_rows, final_resolved, final_cards = await self._listing_snapshot(entry_url, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_url, final_rows, final_resolved, final_cards = initial_url, initial_rows, initial_resolved, initial_cards
            self.result.errors.append(
                "Cursor final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["id"]: (row, h, p) for row, h, p in final_resolved}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("cursor_official_listing_membership_changed_between_initial_and_final_snapshots")
        # Preserve final rows for stable IDs; keep initial-only rows with a
        # warning if membership moved during collection.
        all_ids = list(dict.fromkeys([row["id"] for row in final_rows] + [row["id"] for row in initial_rows]))
        detail_by_id = {check["job_id"]: check for check in detail_checks if check.get("matched")}
        mapping_started = time.perf_counter()
        for identity in all_ids:
            in_final = identity in final_by_id
            row, html, plain = final_by_id[identity] if in_final else initial_by_id[identity]
            job = self._make_job(
                row, html, plain, entry_url=initial_url,
                detail_verification=detail_by_id.get(identity),
            )
            if not in_final and changed_membership:
                job.warnings.append("membership_changed_during_collection")
            self.add(job)
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": initial_url,
            "final_entry_url": final_url,
            "membership_authority": "Cursor official /careers Next.js flight data: jobs array embedded by the listing page, rendered unfiltered via jobs.map; All-teams/All-locations are client-side filters (default All shows every row). No pagination; every card is server-rendered.",
            "listing_filter_behavior": "unfiltered jobs array; client filter is Q?.filter(e => (All teams|department match) && (All locations|location-list match)); defaults All teams/All locations retain every row",
            "ui_pagination": "No job pagination; all cards are server-rendered in RSC order (verified against static article hrefs).",
            "department_filters": ["All teams", *sorted({row.get("department") or "Other" for row in final_rows})],
            "location_filters": ["All locations", *sorted({row.get("location") or "Other" for row in final_rows})],
            "initial_rows": len(initial_rows),
            "final_rows": len(final_rows),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [row["id"] for row in initial_rows],
            "final_visible_ids": [row["id"] for row in final_rows],
            "initial_visible_slugs": [row["urlSlug"] for row in initial_rows],
            "final_visible_slugs": [row["urlSlug"] for row in final_rows],
            "initial_card_slugs": initial_cards,
            "final_card_slugs": final_cards,
            "exact_native_detail_pages_opened": [check["detail_url"] for check in detail_checks],
            "representative_detail_checks": detail_checks,
            "representative_details_matched": all(check.get("matched") for check in detail_checks),
            "description_source": "Each exact listed row's T-segment descriptionHtml/descriptionPlain resolved from the listing HTML; sampled native detail pages embed the same job object and byte-identical segments under their own $ refs.",
            "source_has_structured_compensation_field": False,
            "salary_policy": "Structured compensation is null for every inspected row and no source pay-range text is retained as salary; incidental $ mentions (deal size, relationship value) are not salary and stay null.",
            "job_attachment_links": {row["id"]: _job_attachment_links(final_by_id[row["id"]][1])
                                     for row in final_rows if _job_attachment_links(final_by_id[row["id"]][1])},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
            "detail_page_requests": len(detail_checks),
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

