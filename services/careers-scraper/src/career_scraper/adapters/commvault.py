"""Commvault's own embedded Greenhouse listing and native job detail journey.

Membership authority is the server-rendered ``/careers/jobs`` page published by
Commvault: its ``greenhouse_list`` JSON, its ``.job-card`` elements and its
``All (N)`` filter counts all describe the same unfiltered set. The shipped
``greenhouse.js`` asset only filters those already-rendered cards in the
browser; it issues no job-listing request and no pagination exists.

Each card points to Commvault's native ``/careers/jobs/<job_id>`` route. The
native detail page server-renders the posting: ``job__body`` holds a shared
``content-intro`` template, the job-specific body, an optional
``content-pay-transparency`` salary block and a shared ``content-conclusion``
template. The job description is the job-specific body only; the three
template blocks are retained verbatim in ``raw`` but are not counted as role
descriptions. The exact ``Apply now`` link on each detail page points to
``https://job-boards.greenhouse.io/commvault/jobs/<job_id>#application-form``;
only that exact per-job URL is retained as the apply link. No ATS board is
enumerated.
"""
import html
import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY_PATH = "/careers"
LISTING_PATH = "/careers/jobs"
JOB_ID_RE = re.compile(r"^\d+$")
GREENHOUSE_LIST_RE = re.compile(
    r"const\s+greenhouse_list\s*=\s*(\[.*?\])\s*//# sourceURL", re.S
)
GH_APPLY_RE = re.compile(
    r"^https://job-boards\.greenhouse\.io/commvault/jobs/(\d+)#application-form$"
)
GH_JS_SRC_RE = re.compile(
    r"commvault-greenhouse/assets/greenhouse\.js\?ver=([0-9.]+)"
)


def compact_body_hash(html_value):
    """NFC/whitespace-free SHA-256 of a rendered description body."""
    value = unicodedata.normalize("NFC", text_html(html_value))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(entry_url, job_id):
    if not JOB_ID_RE.fullmatch(str(job_id)):
        raise ValueError(f"Commvault Greenhouse job ID is not numeric: {job_id!r}")
    return urljoin(entry_url, f"{LISTING_PATH}/{job_id}")


def greenhouse_apply_url(job_id):
    if not JOB_ID_RE.fullmatch(str(job_id)):
        raise ValueError(f"Commvault Greenhouse job ID is not numeric: {job_id!r}")
    return f"https://job-boards.greenhouse.io/commvault/jobs/{job_id}#application-form"


def _ws(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class Commvault(Adapter):
    """Follow only Commvault's embedded listing and its exact native details."""

    VERIFICATION_REVISION = "commvault.embedded-greenhouse-list-and-native-detail.1"
    COMPANY_HOSTS = {"www.commvault.com"}
    GH_BOARD_HOST = "job-boards.greenhouse.io"
    GH_BOARD = "commvault"

    # -- listing ---------------------------------------------------------
    @staticmethod
    def _assert_company_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Commvault.COMPANY_HOSTS:
            raise ValueError(f"Commvault company page left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Commvault company page asset has an unexpected path: {url}")

    @staticmethod
    def _listing_js_url(listing_html):
        match = GH_JS_SRC_RE.search(listing_html)
        if not match:
            raise ValueError("Commvault listing page no longer ships its greenhouse.js asset")
        return (
            "https://www.commvault.com/wp-content/mu-plugins/commvault-greenhouse/assets/"
            f"greenhouse.js?ver={match.group(1)}"
        )

    @staticmethod
    def _parse_listing(listing_html, snapshot_name):
        match = GREENHOUSE_LIST_RE.search(listing_html)
        if not match:
            raise ValueError(
                f"Commvault {snapshot_name} listing page no longer embeds greenhouse_list"
            )
        try:
            rows = json.loads(match.group(1))
        except ValueError as exc:
            raise ValueError(f"Commvault {snapshot_name} greenhouse_list is not valid JSON") from exc
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Commvault {snapshot_name} greenhouse_list is empty or malformed")
        ids = [row.get("job_id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(i, str) or not JOB_ID_RE.fullmatch(i) for i in ids):
            raise ValueError(f"Commvault {snapshot_name} listing contains a missing or invalid job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Commvault {snapshot_name} listing repeats a company-published ID")
        if any(not row.get("title") for row in rows):
            raise ValueError(f"Commvault {snapshot_name} listing contains an untitled job")

        soup = BeautifulSoup(listing_html, "lxml")
        cards = soup.select(".job-card[data-jobid]")
        card_ids = [card.get("data-jobid") for card in cards]
        if card_ids != ids:
            raise ValueError(
                f"Commvault {snapshot_name} rendered job-cards do not reconcile "
                "with its embedded greenhouse_list (order/IDs differ)"
            )
        all_counts = {int(n) for n in re.findall(r"All \((\d+)\)", listing_html)}
        if all_counts != {len(rows)}:
            raise ValueError(
                f"Commvault {snapshot_name} filter 'All (N)' count does not equal "
                f"its embedded listing size: {sorted(all_counts)} vs {len(rows)}"
            )
        return rows, card_ids

    # -- detail ----------------------------------------------------------
    @staticmethod
    def _parse_detail(detail_html, expected_id, listing_row):
        soup = BeautifulSoup(detail_html, "lxml")
        article = soup.select_one("article.job[data-jobid]")
        if article is None or article.get("data-jobid") != expected_id:
            raise ValueError(
                f"Commvault native detail page for {expected_id} has no matching "
                "article[data-jobid]"
            )
        h1 = soup.select_one(".job__heading h1")
        detail_title = h1.get_text(" ", strip=True) if h1 else ""
        listing_title = listing_row.get("title") or ""
        if _ws(detail_title) != _ws(listing_title):
            raise ValueError(
                f"title conflict for {expected_id}: listing {listing_title!r} "
                f"!= detail {detail_title!r}"
            )
        body = soup.select_one(".job__body")
        if body is None:
            raise ValueError(f"Commvault native detail {expected_id} has no .job__body")
        intro = body.select_one(".content-intro")
        conclusion = body.select_one(".content-conclusion")
        pay_block = body.select_one(".content-pay-transparency")

        # Job-specific description: job__body minus template/pay blocks.
        kept = []
        for child in list(body.children):
            if isinstance(child, str):
                if child.strip():
                    kept.append(child)
                continue
            classes = getattr(child, "get", lambda *a, **k: None)("class") or []
            if any(c in classes for c in ("content-intro", "content-conclusion",
                                          "content-pay-transparency")):
                continue
            kept.append(child.decode() if hasattr(child, "decode") else str(child))
        description_html = "".join(kept).strip()

        # Pay transparency block -> salary.
        salary = None
        pay_boilerplate = None
        if pay_block is not None:
            pay_text = pay_block.select_one(".pay-input .description")
            pay_boilerplate = pay_text.get_text(" ", strip=True) if pay_text else None
            spans = [span.get_text(" ", strip=True) for span in pay_block.select(".pay-range span")
                     if span.get_text(" ", strip=True) and "divider" not in (span.get("class") or [])]
            spans = [s for s in spans if s != "—"]
            if len(spans) >= 2:
                salary = {
                    "source_text": f"{spans[0]} — {spans[-1]}",
                    "source_field": "detail.content-pay-transparency.pay-range",
                    "semantics": (
                        "Published minimum/maximum base salary range text; preserve "
                        "source text without conversion to total compensation."
                    ),
                }

        applies = [
            a.get("href") for a in soup.select("article.job a[href]")
            if str(a.get("href") or "").startswith(
                f"https://{Commvault.GH_BOARD_HOST}/{Commvault.GH_BOARD}/")
        ]
        expected_apply = greenhouse_apply_url(expected_id)
        exact_applies = [u for u in applies if GH_APPLY_RE.fullmatch(u or "")]
        if exact_applies != [expected_apply]:
            raise ValueError(
                f"Commvault native detail {expected_id} does not expose the exact "
                f"published Greenhouse apply link: {applies}"
            )

        tags = [span.get_text(" ", strip=True) for span in soup.select(".job__meta .tag")]
        return {
            "title": listing_title,
            "detail_title": detail_title,
            "description_html": description_html,
            "intro_html": intro.decode() if intro is not None else None,
            "conclusion_html": conclusion.decode() if conclusion is not None else None,
            "pay_boilerplate": pay_boilerplate,
            "salary": salary,
            "apply_url": expected_apply,
            "heading_tags": tags,
            "attachment_links": _job_attachment_links(description_html),
        }

    def _make_job(self, row, detail, *, entry_url, listing_js_url,
                  membership_changed=False):
        job_id = row["job_id"]
        description_html = detail["description_html"] or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{job_id}: Commvault public detail description is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(
                f"{job_id}: Commvault public detail description is implausibly short")
        unescaped = html.unescape(row.get("department") or "")
        raw = {
            "listing": row,
            "detail": {
                "native_detail_url": native_job_url(entry_url, job_id),
                "intro_html": detail["intro_html"],
                "intro_text": text_html(detail["intro_html"]),
                "conclusion_html": detail["conclusion_html"],
                "conclusion_text": text_html(detail["conclusion_html"]),
                "pay_boilerplate": detail["pay_boilerplate"],
                "heading_tags": detail["heading_tags"],
                "detail_title": detail["detail_title"],
                "attachment_links": detail["attachment_links"],
            },
            "source_ui": {
                "listing_js_url": listing_js_url,
                "apply_url": detail["apply_url"],
                "attachment_links": detail["attachment_links"],
                "salary_from_detail": detail["salary"],
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=native_job_url(entry_url, job_id),
            job_id=job_id,
            requisition_id=row.get("requisition_id"),
            apply_url=detail["apply_url"],
            description=description_html,
            method="commvault.official_careers_embed.native_detail_job_body",
            description_url=native_job_url(entry_url, job_id),
            locations=[{"location": row.get("location"), "country": row.get("country"),
                        "state": row.get("state")}],
            department=unescaped,
            employment_type=row.get("hiring_type"),
            workplace_type=("Remote" if row.get("remote_position") == "Yes" else "On-site"),
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            employer=None,
            salary=detail["salary"],
        )
        job.description_text = description_text
        job.record_identity = f"commvault:{job_id}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    # -- run -------------------------------------------------------------
    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in self.COMPANY_HOSTS
                or parts.path.rstrip("/") != ENTRY_PATH):
            raise ValueError("Commvault dedicated flow must start from its official /careers entry")
        entry = await self.http.get(entry_url)
        self._assert_company_url(entry.url, path_prefix="/careers")
        if "/careers/jobs" not in entry.text:
            raise ValueError("Commvault /careers entry no longer links to its /careers/jobs listing")

        listing_url = urljoin(entry_url, LISTING_PATH)
        list_started = time.perf_counter()
        initial_html = (await self.http.get(listing_url)).text
        initial_rows, initial_card_ids = self._parse_listing(initial_html, "initial")
        initial_by_id = {row["job_id"]: row for row in initial_rows}
        listing_js_url = self._listing_js_url(initial_html)
        self._assert_company_url(listing_js_url,
                                 path_prefix="/wp-content/mu-plugins/commvault-greenhouse/")
        js_started = time.perf_counter()
        js_response = await self.http.get(listing_js_url)
        listing_js = js_response.text
        if ("greenhouse_list" not in listing_js or "fetch(" in listing_js or "XMLHttpRequest" in listing_js):
            raise ValueError("Commvault greenhouse.js no longer filters the embedded list client-side")
        js_seconds = time.perf_counter() - js_started
        listing_seconds = time.perf_counter() - list_started

        # Native details for every listed ID (hydrate records detail timings).
        stubs = []
        for identity in initial_card_ids:
            row = initial_by_id[identity]
            stub = self.job(
                {"listing": row},
                title=row["title"],
                url=native_job_url(entry_url, identity),
                job_id=identity,
                requisition_id=row.get("requisition_id"),
            )
            stub.record_identity = f"commvault:{identity}"
            stubs.append(stub)

        async def detail(job):
            job_id = job.job_id
            response = await self.http.get(native_job_url(entry_url, job_id))
            self._assert_company_url(response.url, path_prefix="/careers/jobs/")
            if urlsplit(str(response.url)).path.rstrip("/") != f"{LISTING_PATH}/{job_id}":
                raise ValueError("Commvault detail route redirected away from its exact job ID")
            parsed = self._parse_detail(response.text, job_id, job.raw["listing"])
            if parsed["attachment_links"]:
                self.result.errors.append(
                    f"{job_id}: published role attachment links are present but no full "
                    "attachment text was captured"
                )
            built = self._make_job(job.raw["listing"], parsed, entry_url=entry_url,
                                   listing_js_url=listing_js_url)
            built.record_identity = f"commvault:{job_id}"
            return built

        await self.hydrate(stubs, detail)
        jobs_by_id = {job.job_id: job for job in stubs}
        # Preserve listing order (hydrate returns in order already).
        ordered = [jobs_by_id[i] for i in initial_card_ids]

        final_snapshot_verified = True
        try:
            final_html = (await self.http.get(listing_url)).text
            final_rows, final_card_ids = self._parse_listing(final_html, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_rows, final_card_ids = initial_rows, initial_card_ids
            self.result.errors.append(
                "Commvault final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["job_id"]: row for row in final_rows}
        changed_membership = (
            final_snapshot_verified
            and (set(final_by_id) != set(initial_by_id) or final_card_ids != initial_card_ids)
        )
        if changed_membership:
            self.result.errors.append(
                "commvault_official_listing_membership_changed_between_initial_and_final_snapshots")

        self.result.jobs = ordered
        self.result.seen = {job.key for job in ordered}
        self.result.dirty_jobs = list(ordered)
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        self.result.listing_complete = bool(
            final_snapshot_verified and not changed_membership and not self.result.errors
        )

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_url": listing_url,
            "listing_js_url": listing_js_url,
            "membership_authority": (
                "Commvault's own /careers/jobs page: embedded greenhouse_list JSON, "
                "server-rendered .job-card elements and the 'All (N)' filter count "
                "describe the same unfiltered set; greenhouse.js only filters those "
                "already-rendered cards client-side (no listing fetch, no pagination)"
            ),
            "listing_filter_behavior": (
                "unfiltered embedded rows are filtered in the browser by department, "
                "hiring_type, country, state and remote_position; internship filtering "
                "is a job_profile_code special case (TP1000/TP1005) over the same list"
            ),
            "ui_pagination": (
                "No job pagination: every embedded row is server-rendered as a card "
                "on the single /careers/jobs page"
            ),
            "initial_rows": len(initial_rows),
            "final_rows": len(final_rows),
            "initial_visible_ids": initial_card_ids,
            "final_visible_ids": final_card_ids,
            "initial_snapshot_verified": True,
            "final_snapshot_verified": final_snapshot_verified,
            "description_source": (
                "Each exact native /careers/jobs/<job_id> page's .job__body "
                "job-specific body (shared content-intro/content-conclusion "
                "templates and the pay-transparency block retained in raw, not "
                "counted as description)"
            ),
            "source_has_posting_dates": False,
            "salary_policy": (
                "A published pay-transparency range is retained with its source "
                "text and base-salary-range semantics; no conversion is performed."
            ),
            "job_attachment_links": {
                job.job_id: job.raw.get("detail", {}).get("attachment_links")
                for job in ordered if job.raw.get("detail", {}).get("attachment_links")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["template_asset_phase_seconds"] = round(js_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = (
            self.result.metadata["collection_wall_seconds"]
        )
        return self.result
