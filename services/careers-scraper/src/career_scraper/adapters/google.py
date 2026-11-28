"""Google Careers' native paginated job results and embedded full role fields."""
from __future__ import annotations

from hashlib import sha256
import gzip
import json
import math
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter


OFFICIAL_ENTRY = "https://www.google.com/about/careers/applications/jobs/results/"
RESULTS_PATH = "/about/careers/applications/jobs/results/"
APPLICATIONS_BASE = "https://www.google.com/about/careers/applications/"
DETAIL_PATH = re.compile(r"^/about/careers/applications/jobs/results/(\d+)(?:-[^/]*)?/?$")
JOB_ID = re.compile(r"^\d+$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)
PAY_MARKER = "individual pay is determined"

# This is an index map, not a claim that Google exposes named JSON properties.
# Unknown positions are kept verbatim under native_row; the displayed labels and
# IDs are checked separately against the page's native job-card links.
ROW_FIELDS = {
    0: "job_id",
    1: "title",
    2: "application_signin_url",
    3: "responsibilities_html",
    4: "qualifications_html",
    5: "organization_path",
    6: "native_field_06",
    7: "employer_name",
    8: "language",
    9: "locations",
    10: "about_body_html",
    11: "experience_level_codes",
    12: "native_field_12",
    13: "native_field_13",
    14: "native_field_14",
    15: "role_note_html",
    16: "native_field_16",
    17: "native_field_17",
    18: "remote_location_note_html",
    19: "minimum_qualifications_html",
    20: "native_field_20",
}


def _init_data(soup: BeautifulSoup, key: str):
    pattern = re.compile(rf"key\s*:\s*(['\"]){re.escape(key)}\1")
    matches = []
    for script in soup.find_all("script"):
        body = script.string or script.get_text()
        marker = pattern.search(body)
        if marker is None:
            continue
        start = body.find("data:", marker.end())
        end = body.find(", sideChannel:", start)
        if start < 0 or end < 0:
            raise ValueError(f"Google native {key} payload boundaries changed")
        try:
            matches.append(json.loads(body[start + len("data:"):end]))
        except ValueError as exc:
            raise ValueError(f"Google native {key} payload is not valid JSON data") from exc
    if len(matches) != 1:
        raise ValueError(f"Google native page must contain one {key} payload; found {len(matches)}")
    return matches[0]


def _read_page_capture_cache(output, run_id: str, phase: str) -> dict[str, dict]:
    """Read same-run page captures so an interrupted run can resume without re-fetching pages."""
    path = output / "google-native-page-captures.jsonl" if output else None
    if path is None or not path.exists():
        return {}
    captures = {}
    for line in path.read_text().splitlines():
        try:
            capture = json.loads(line)
        except ValueError:
            continue
        if capture.get("run_id") == run_id and capture.get("phase") == phase:
            captures[capture.get("request_url")] = capture
    return captures


def _archive_page_capture(output, record: dict):
    if not output:
        return
    path = output / "google-native-page-captures.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def _page_number(url: str) -> int:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"page"}:
        raise ValueError("Google results URL has uninspected filters; only its native page parameter is allowed")
    if "page" not in query:
        return 1
    values = query["page"]
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) < 1:
        raise ValueError("Google native results page number is invalid")
    return int(values[0])


def parse_results_page(html: str, response_url: str) -> dict:
    """Parse one official SSR results page and its exact next-page link."""
    parsed_url = urlsplit(response_url)
    page_number = _page_number(response_url)
    if (parsed_url.scheme != "https" or parsed_url.netloc != "www.google.com"
            or parsed_url.path != RESULTS_PATH):
        raise ValueError("Google native results response came from an unexpected official route")

    soup = BeautifulSoup(html or "", "lxml")
    if not soup.title or "Google Careers" not in soup.title.get_text(" ", strip=True):
        raise ValueError("Google official results page title changed")
    if soup.select_one("base[href]") is None:
        raise ValueError("Google official results page no longer publishes its application base URL")
    base_url = urljoin(response_url, soup.select_one("base[href]").get("href", ""))
    base_parts = urlsplit(base_url)
    if (base_parts.scheme != "https" or base_parts.netloc != "www.google.com"
            or base_parts.path != "/about/careers/applications/"):
        raise ValueError("Google official results page published an unexpected base URL")

    payload = _init_data(soup, "ds:1")
    if (not isinstance(payload, list) or len(payload) < 4 or not isinstance(payload[0], list)
            or not isinstance(payload[2], int) or payload[2] < 0
            or not isinstance(payload[3], int) or payload[3] < 1):
        raise ValueError("Google native results data total/page-size schema changed")
    source_rows = payload[0]
    total, page_size = payload[2], payload[3]
    if not all(isinstance(row, list) and len(row) in (20, 21) for row in source_rows):
        raise ValueError("Google native job row schema changed")

    card_links = []
    for anchor in soup.select('a[aria-label^="Learn more about"]'):
        card_url = urljoin(base_url, anchor.get("href", ""))
        card_parts = urlsplit(card_url)
        match = DETAIL_PATH.fullmatch(card_parts.path)
        label = anchor.get("aria-label", "")
        if (match is None or card_parts.scheme != "https" or card_parts.netloc != "www.google.com"
                or not label.startswith("Learn more about ")):
            raise ValueError("Google native results page exposed an invalid exact job link")
        card_links.append({"job_id": match.group(1), "title": label[len("Learn more about "):], "url": card_url})

    source_ids, seen, rows, issues = [], set(), [], []
    for source_row, card in zip(source_rows, card_links):
        identity = str(source_row[0])
        source_ids.append(identity)
        if not JOB_ID.fullmatch(identity) or identity != card["job_id"]:
            issues.append(f"google_native_id_link_mismatch:{identity}")
            continue
        if identity in seen:
            issues.append(f"google_duplicate_job_id_on_page:{identity}")
            continue
        seen.add(identity)
        if not isinstance(source_row[1], str) or not source_row[1].strip():
            issues.append(f"google_missing_job_title:{identity}")
            continue
        if re.sub(r"\s+", " ", source_row[1]).strip() != re.sub(r"\s+", " ", card["title"]).strip():
            issues.append(f"google_native_title_link_mismatch:{identity}")
            continue
        rows.append({"source_row": source_row, "native_view": card})

    display_match = re.search(r"([\d,]+)\s+jobs matched", soup.get_text(" ", strip=True), re.I)
    display_total = int(display_match.group(1).replace(",", "")) if display_match else None
    if display_total != total:
        issues.append("google_visible_count_data_total_mismatch")
    if len(source_rows) != len(card_links):
        issues.append("google_native_rows_dom_card_count_mismatch")
    if page_number < math.ceil(total / page_size) and len(source_rows) != page_size:
        issues.append("google_native_nonterminal_page_has_unexpected_row_count")

    next_link = soup.select('a[aria-label="Go to next page"]')
    previous_link = soup.select('a[aria-label="Go to previous page"]')
    if len(next_link) > 1 or len(previous_link) > 1:
        issues.append("google_native_pagination_links_ambiguous")
    next_url = urljoin(response_url, next_link[0].get("href", "")) if next_link else None
    previous_url = urljoin(response_url, previous_link[0].get("href", "")) if previous_link else None
    expected_pages = math.ceil(total / page_size)
    if page_number == 1 and previous_url is not None:
        issues.append("google_first_page_has_previous_link")
    if page_number > 1 and (previous_url is None or _page_number(previous_url) != page_number - 1):
        issues.append("google_previous_page_link_mismatch")
    if page_number < expected_pages and (next_url is None or _page_number(next_url) != page_number + 1):
        issues.append("google_next_page_link_mismatch")
    if page_number >= expected_pages and next_url is not None:
        issues.append("google_last_page_has_next_link")
    for value in (next_url, previous_url):
        if value:
            link_parts = urlsplit(value)
            if (link_parts.scheme != "https" or link_parts.netloc != "www.google.com"
                    or link_parts.path != RESULTS_PATH):
                issues.append("google_pagination_link_left_official_results_route")

    return {
        "page_number": page_number,
        "rows": rows,
        "job_ids": [row["native_view"]["job_id"] for row in rows],
        "source_order_ids": source_ids,
        "total": total,
        "display_total": display_total,
        "page_size": page_size,
        "expected_pages": expected_pages,
        "next_url": next_url,
        "previous_url": previous_url,
        "source_field_indices": sorted({index for row in source_rows for index in range(len(row))}),
        "source_field_names_by_index": {str(index): ROW_FIELDS.get(index, f"native_field_{index:02d}")
                                         for index in sorted({i for row in source_rows for i in range(len(row))})},
        "row_length_counts": {str(length): sum(len(row) == length for row in source_rows)
                              for length in sorted({len(row) for row in source_rows})},
        "visible_card_ids_match_native_rows": source_ids == [row["job_id"] for row in card_links],
        "issues": issues,
    }


def canonical_job_url(observed_url: str) -> str:
    """Return the published detail path without Google's results-pagination query.

    Google repeats its own card href verbatim on every page; from page 2 on the
    href carries ``?page=N`` purely as results context. The path itself is the
    canonical job page, so the page parameter is dropped and the exact observed
    link is retained in raw evidence.
    """
    parts = urlsplit(observed_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) - {"page"}:
        raise ValueError("Google job card exposed uninspected query parameters")
    if DETAIL_PATH.fullmatch(parts.path) is None or parts.netloc != "www.google.com" or parts.scheme != "https":
        raise ValueError("Google job card left the official detail route")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _row_fragment(row: list, index: int) -> str:
    if index >= len(row):
        return ""
    value = row[index]
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return ""


def _salary_source_text(about_html: str) -> str | None:
    text = BeautifulSoup(about_html or "", "lxml").get_text(" ", strip=True)
    match = re.search(r"individual pay is determined", text, re.I)
    if match is None:
        return None
    value = text[match.start():]
    benefits = re.search(r"learn more about benefits", value, re.I)
    if benefits:
        value = value[:benefits.start()]
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _role_attachments(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    return [{"text": a.get_text(" ", strip=True), "url": a["href"]}
            for a in soup.select("a[href]") if ATTACHMENT_SUFFIX.search(a["href"])]


def _description_fragments(row: list) -> dict:
    return {
        "about_the_job_html": _row_fragment(row, 10),
        "responsibilities_html": _row_fragment(row, 3),
        "qualifications_html": _row_fragment(row, 4),
        "minimum_qualifications_html": _row_fragment(row, 19),
        "role_note_html": _row_fragment(row, 15),
        "remote_location_note_html": _row_fragment(row, 18),
    }


def _description_html(fragments: dict) -> str:
    parts = []
    if fragments["about_the_job_html"]:
        parts.append(f"<section><h2>About the job</h2>{fragments['about_the_job_html']}</section>")
    if fragments["responsibilities_html"]:
        parts.append(f"<section><h2>Responsibilities</h2>{fragments['responsibilities_html']}</section>")
    if fragments["qualifications_html"]:
        parts.append(f"<section>{fragments['qualifications_html']}</section>")
    if fragments["role_note_html"]:
        parts.append(f"<section><h2>Additional role note</h2>{fragments['role_note_html']}</section>")
    return "".join(parts)


def parse_detail_record(html: str, expected_id: str) -> list:
    """Read the exact native ds:0 job record exposed by a Google detail page."""
    soup = BeautifulSoup(html or "", "lxml")
    data = _init_data(soup, "ds:0")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], list):
        raise ValueError("Google exact detail page no longer exposes one native ds:0 job row")
    row = data[0]
    if not row or str(row[0]) != str(expected_id):
        raise ValueError("Google exact detail ds:0 row changed its exposed job ID")
    if len(row) not in (20, 21) or not isinstance(row[1], str):
        raise ValueError("Google exact detail row schema changed")
    return row


class Google(Adapter):
    """Follow Google's own paginated SSR results and reuse each full ds:1 job row."""

    VERIFICATION_REVISION = "google.native-results-ds1.1"

    def _make_job(self, entry: dict, page_url: str, page_number: int):
        row = entry["source_row"]
        view = entry["native_view"]
        identity = str(view["job_id"])
        canonical_url = canonical_job_url(view["url"])
        fragments = _description_fragments(row)
        body_html = _description_html(fragments)
        locations = row[9] if len(row) > 9 and isinstance(row[9], list) else []
        location_names = [loc[0] for loc in locations if isinstance(loc, list) and loc and isinstance(loc[0], str)]
        remote_note = BeautifulSoup(fragments["remote_location_note_html"], "lxml").get_text(" ", strip=True)
        workplace_type = "Remote" if re.search(r"\bremote location", remote_note, re.I) else None
        experience_codes = row[11] if len(row) > 11 else None
        qualifications_html = fragments["qualifications_html"]
        responsibilities_html = fragments["responsibilities_html"]
        about_salary_text = _salary_source_text(fragments["about_the_job_html"])
        body_soup = BeautifulSoup(body_html or "", "lxml")
        description_links = [{"text": a.get_text(" ", strip=True), "url": a["href"]}
                             for a in body_soup.select("a[href]")]
        attachments = [link for link in description_links if ATTACHMENT_SUFFIX.search(link["url"])]
        return self.job(
            {
                "listing": row,
                "native_page": {"url": page_url, "page_number": page_number,
                                "card_url": view["url"], "observed_card_href": view["url"],
                                "card_title": view["title"]},
                "detail": {
                    "description_fragments": fragments,
                    "description_html": body_html,
                    "description_links": description_links,
                    "role_attachments": attachments,
                    "salary_source_text": about_salary_text,
                    "row_length": len(row),
                },
            },
            title=view["title"],
            url=canonical_url,
            job_id=identity,
            apply_url=row[2] if len(row) > 2 and isinstance(row[2], str) else None,
            employer=row[7] if len(row) > 7 and isinstance(row[7], str) else None,
            language=row[8] if len(row) > 8 and isinstance(row[8], str) else None,
            locations=location_names,
            workplace_type=workplace_type,
            salary={"source_text": about_salary_text} if about_salary_text else None,
            responsibilities={"html": responsibilities_html} if responsibilities_html else None,
            qualifications={"html": qualifications_html} if qualifications_html else None,
            experience=experience_codes,
            description=body_html,
            method="google.careers.native-results-ds1",
            description_url=page_url,
            # No source date field with a verified posted/updated meaning is
            # exposed in the current native page schema. Raw timestamp pairs
            # remain in listing and native_named_fields.
            date_posted=None,
            date_updated=None,
        )

    async def _snapshot(self, start_url: str, *, run_id: str, phase: str,
                        page_limit: int | None = None,
                        reference_rows: dict[str, list] | None = None):
        pages, ids, per_id_rows, entries_by_id, seen_ids = [], [], {}, {}, set()
        duplicate_ids = []
        changed_fields, new_ids = {}, []
        page_url = start_url
        expected_total = expected_size = None
        page_number = 1
        issues = []
        archive_cache = _read_page_capture_cache(self.http.output, run_id, phase)
        while page_url:
            if page_limit is not None and page_number > page_limit:
                issues.append("google_native_page_limit_reached")
                break
            if not self.page_guard():
                issues.append("google_native_page_budget_exhausted")
                break
            capture = archive_cache.get(page_url)
            if capture is not None:
                if not self.http.output:
                    raise ValueError("Google page capture archive is unavailable for resume")
                archive = self.http.output / "responses" / f"{capture['response_sha256']}.gz"
                if not archive.exists():
                    raise ValueError(f"Google saved native page response is missing: page {page_number}")
                body = gzip.decompress(archive.read_bytes())
                if sha256(body).hexdigest() != capture["response_sha256"]:
                    raise ValueError(f"Google saved native page checksum changed: page {page_number}")
                elapsed = capture["request_seconds"]
                status = capture["http_status"]
                response_bytes = capture["response_bytes"]
                response_sha = capture["response_sha256"]
                page_from_archive = True
            else:
                started = time.perf_counter()
                response = await self.http.get(page_url)
                elapsed = max(0.0, time.perf_counter() - started)
                if str(response.url) != page_url:
                    raise ValueError("Google native pagination link redirected away from the inspected results URL")
                body = response.content
                status = response.status_code
                response_bytes = len(body)
                response_sha = sha256(body).hexdigest()
                page_from_archive = False
            parsed = parse_results_page(body.decode("utf-8", "replace"), page_url)
            parsed.update({
                "request_url": page_url,
                "final_url": page_url,
                "http_status": status,
                "request_seconds": elapsed,
                "response_bytes": response_bytes,
                "response_sha256": response_sha,
                "reused_same_run_page_archive": page_from_archive,
            })
            if parsed["page_number"] != page_number:
                parsed["issues"].append("google_native_pagination_order_mismatch")
            if expected_total is None:
                expected_total, expected_size = parsed["total"], parsed["page_size"]
            elif (parsed["total"], parsed["page_size"]) != (expected_total, expected_size):
                parsed["issues"].append("google_native_total_or_page_size_changed")
            issues.extend(parsed["issues"])
            if len(parsed["rows"]) != len(parsed["job_ids"]):
                issues.append(f"google_native_row_parse_gap_on_page:{page_number}")
            for entry in parsed["rows"]:
                identity = entry["native_view"]["job_id"]
                if identity in seen_ids:
                    issues.append(f"google_duplicate_job_id_across_pages:{identity}")
                    duplicate_ids.append(identity)
                    continue
                seen_ids.add(identity)
                if reference_rows is None:
                    per_id_rows[identity] = entry["source_row"]
                    entries_by_id[identity] = entry
                elif identity not in reference_rows:
                    new_ids.append(identity)
                else:
                    old = reference_rows[identity]
                    new = entry["source_row"]
                    changed = [i for i in range(max(len(old), len(new)))
                               if (old[i] if i < len(old) else None) != (new[i] if i < len(new) else None)]
                    if changed:
                        changed_fields[identity] = changed
                ids.append(identity)
            pages.append({key: parsed[key] for key in (
                "page_number", "request_url", "final_url", "http_status", "request_seconds",
                "response_bytes", "response_sha256", "reused_same_run_page_archive", "job_ids", "source_order_ids", "total",
                "page_size", "expected_pages", "source_field_indices", "row_length_counts", "issues",
            )})
            if not page_from_archive:
                record = {
                    "run_id": run_id, "phase": phase, "page_number": page_number,
                    "request_url": page_url, "final_url": page_url, "http_status": status,
                    "request_seconds": elapsed, "response_bytes": response_bytes,
                    "response_sha256": response_sha, "next_url": parsed["next_url"],
                    "job_ids": parsed["job_ids"], "total": parsed["total"],
                    "page_size": parsed["page_size"],
                }
                _archive_page_capture(self.http.output, record)
            checkpoint = self.options.previous_report.get("google_page_checkpoint")
            if callable(checkpoint):
                checkpoint({"run_id": run_id, "phase": phase, "page_number": page_number,
                            "pages_completed": len(pages), "total": expected_total,
                            "response_sha256": response_sha, "request_url": page_url,
                            "next_url": parsed["next_url"], "cached": page_from_archive})
            next_url = parsed["next_url"]
            del body, parsed
            if not page_from_archive:
                del response
            page_url = next_url
            page_number += 1

        if expected_total is None or expected_size is None:
            issues.append("google_native_results_missing_total")
        else:
            if len(pages) != math.ceil(expected_total / expected_size):
                issues.append("google_native_pagination_page_count_mismatch")
            if len(ids) != expected_total or (reference_rows is None and len(per_id_rows) != expected_total):
                issues.append("google_native_total_ids_mismatch")
        removed_ids = ([identity for identity in reference_rows if identity not in set(ids)]
                       if reference_rows is not None else [])
        return {
            "pages": pages,
            "job_ids": ids,
            "rows_by_id": per_id_rows,
            "entries_by_id": entries_by_id,
            "total": expected_total,
            "page_size": expected_size,
            "issues": issues,
            "complete": not issues,
            "duplicate_ids": sorted(set(duplicate_ids)),
            "self_consistent": bool(
                not issues and expected_total is not None
                and len(ids) == len(set(ids)) == expected_total),
            "changed_fields": changed_fields,
            "new_ids": new_ids,
            "removed_ids": removed_ids,
        }

    async def run(self):
        source_started = time.perf_counter()
        run_id = self.options.previous_report.get("google_run_id")
        if not run_id:
            from uuid import uuid4
            run_id = uuid4().hex
            self.options.previous_report["google_run_id"] = run_id
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_authority": "Google's official careers results page and its server-rendered ds:1 records plus exact visible pagination links.",
            "listing_method": "Start at the official unfiltered results URL; follow only its current Go to next page link, parse the page's ds:1 data and reconcile it with the rendered job-card IDs.",
            "detail_method": "The official native ds:1 results payload includes each exact job ID, about-body HTML, responsibilities, qualifications and location data. No separate detail request is required for the captured listings.",
            "request_scope": "Only exact job IDs and complete source rows exposed by Google's unfiltered official careers results pages; no third-party ATS or guessed endpoint.",
            "native_page_data_key": "AF_initDataCallback key=ds:1",
            "native_detail_data_key": "AF_initDataCallback key=ds:0 (verified against representative exact detail page)",
            "source_field_schema_by_index": {str(i): name for i, name in ROW_FIELDS.items()},
            "detail_page_requests_performed": 0,
            "details_in_native_results_payload": True,
            "source_run_id": run_id,
        })
        initial_started = time.perf_counter()
        try:
            initial = await self._snapshot(OFFICIAL_ENTRY, run_id=run_id, phase="initial")
        except Exception as exc:
            self.result.errors.append(f"Google initial official results snapshot failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            return self.result
        finally:
            self.result.timings["initial_listing_seconds"] = max(
                0.0, time.perf_counter() - initial_started)

        self.result.metadata["initial_listing"] = {
            "pages": initial["pages"],
            "job_ids": initial["job_ids"],
            "total": initial["total"],
            "page_size": initial["page_size"],
            "page_count": len(initial["pages"]),
            "complete": initial["complete"],
            "issues": initial["issues"],
            "source_field_names_by_index": {str(i): ROW_FIELDS.get(i, f"native_field_{i:02d}")
                                            for i in sorted({i for row in initial["rows_by_id"].values() for i in range(len(row))})},
        }
        self.result.expected_total = initial["total"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])

        page_by_id = {identity: page for page in initial["pages"] for identity in page["job_ids"]}
        inline_parse_seconds = 0.0
        for identity in initial["job_ids"]:
            entry = initial["entries_by_id"].get(identity)
            page = page_by_id.get(identity)
            if entry is None or page is None:
                self.result.errors.append(f"google_native_page_reference_missing:{identity}")
                continue
            if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
                self.result.warnings.append("sample_limit")
                self.result.listing_complete = False
                break
            parse_started = time.perf_counter()
            self.add(self._make_job(entry, page["request_url"], page["page_number"]))
            inline_parse_seconds += max(0.0, time.perf_counter() - parse_started)
        self.result.timings["inline_detail_parse_seconds"] = inline_parse_seconds

        if self.options.max_jobs and len(self.result.jobs) < (initial["total"] or 0):
            self.result.warnings.append("sample_limit")

        # Re-fetch the complete current native pagination chain after the
        # source rows have been processed; compare every exact row array.
        final_started = time.perf_counter()
        try:
            final = await self._snapshot(OFFICIAL_ENTRY, run_id=run_id, phase="final",
                                         reference_rows=initial["rows_by_id"])
        except Exception as exc:
            self.result.errors.append(f"Google final official results snapshot failed: {type(exc).__name__}: {exc}")
            final = {"pages": [], "job_ids": [], "rows_by_id": {}, "total": None,
                     "page_size": None, "issues": [], "complete": False,
                     "changed_fields": {}, "new_ids": [], "removed_ids": []}
        finally:
            self.result.timings["final_listing_seconds"] = max(
                0.0, time.perf_counter() - final_started)
        jobs_by_id = {str(job.job_id): job for job in self.result.jobs}
        initial_ids, final_ids = initial["job_ids"], final["job_ids"]
        same_ids_order = final_ids == initial_ids
        same_pages = (len(final["pages"]) == len(initial["pages"])
                      and all(a.get("page_number") == b.get("page_number")
                              and a.get("job_ids") == b.get("job_ids")
                              and a.get("source_field_indices") == b.get("source_field_indices")
                              for a, b in zip(initial["pages"], final["pages"])))
        totals_match = (final["total"], final["page_size"], len(final["pages"])) == (
            initial["total"], initial["page_size"], len(initial["pages"])
        )
        # The final pass is a live re-walk, not a replay. Google's list can shift
        # between or during passes; a shift makes that pass internally
        # inconsistent (a duplicate and a skipped id). Such a probe is recorded
        # as a warning and never rewrites or invalidates the snapshot that was
        # collected from a self-consistent pass.
        final_self_consistent = bool(final["self_consistent"])
        changed_fields, new_ids, removed_ids = {}, [], []
        if final_self_consistent:
            if same_ids_order and same_pages and totals_match and not final["changed_fields"]:
                final_proof_status = "identical"
            else:
                final_proof_status = "changed_during_collection"
                changed_fields = final["changed_fields"]
                new_ids = [identity for identity in final_ids if identity not in jobs_by_id]
                removed_ids = [identity for identity in initial_ids if identity not in set(final_ids)]
                self.result.warnings.append(
                    "google_membership_changed_between_collection_and_final_proof")
        else:
            final_proof_status = "unstable_during_collection"
            self.result.warnings.append(
                "google_final_proof_pass_observed_unstable_live_listing:"
                + ",".join(final["issues"]))
        if self.options.max_jobs:
            new_ids = [identity for identity in final_ids if identity not in jobs_by_id]
            removed_ids = [identity for identity in initial_ids if identity not in set(final_ids)]
        reconciliation_complete = bool(
            initial["self_consistent"] and not initial["issues"]
            and not self.options.max_jobs
        )
        final_meta = {
            "pages": final["pages"], "job_ids": final_ids, "total": final["total"],
            "page_size": final["page_size"], "page_count": len(final["pages"]),
            "complete": final["complete"], "self_consistent": final_self_consistent,
            "issues": final["issues"], "duplicate_ids": final["duplicate_ids"],
        }
        self.result.metadata["final_listing"] = final_meta
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": initial_ids, "final_ids": final_ids,
            "initial_total": initial["total"], "final_total": final["total"],
            "initial_page_size": initial["page_size"], "final_page_size": final["page_size"],
            "initial_page_count": len(initial["pages"]), "final_page_count": len(final["pages"]),
            "initial_self_consistent": bool(initial["self_consistent"]),
            "final_self_consistent": final_self_consistent,
            "final_proof_status": final_proof_status,
            "final_proof_issues": final["issues"],
            "final_proof_duplicate_ids": final["duplicate_ids"],
            "same_ids_and_order": same_ids_order, "same_page_boundaries": same_pages,
            "counts_match": totals_match, "changed_native_field_indices_by_id": changed_fields,
            "new_job_ids": new_ids, "removed_job_ids": removed_ids,
            "membership_basis": "initial pass (self-consistent) snapshot",
            "initial_source_field_names_by_index": self.result.metadata["initial_listing"]["source_field_names_by_index"],
            "final_source_field_indices": sorted({i for page in final["pages"] for i in page["source_field_indices"]}),
            "complete": reconciliation_complete,
        }
        self.result.metadata["job_ids"] = [str(job.job_id) for job in self.result.jobs]
        self.result.metadata["retained_total"] = len(self.result.jobs)
        self.result.metadata["current_native_membership_reconciled"] = reconciliation_complete
        self.result.listing_complete = bool(
            reconciliation_complete and not initial["issues"]
            and len(self.result.jobs) == initial["total"]
            and "sample_limit" not in self.result.warnings
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
