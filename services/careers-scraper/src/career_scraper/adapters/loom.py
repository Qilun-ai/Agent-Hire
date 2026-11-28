"""Loom's official careers journey (reviewed 2026-11-07; Round 3 scope
decision 2026-11-08).

The ledger entry ``https://www.loom.com/careers`` is current and official, but
Loom no longer publishes a company-scoped job listing.  It returns a 308
redirect to Atlassian's **parent-wide** all-jobs page::

    https://www.atlassian.com/company/careers/all-jobs?team=&location=&search=Loom

The ``search=Loom`` query is not applied by the server: the SSR page renders
all 342 Atlassian jobs, the search input is empty, and the only filters are
Atlassian's 17 functions and 15 locations.  Typing "Loom" into the rendered
search box returns 13 fuzzy matches (the two Loom-titled roles plus eleven
"Development" roles), so it is a keyword filter, not a Loom organisation scope.

**Round 3 (user scope decision 2026-11-08):** Loom has been absorbed into
Atlassian.  Collect the postings *of Loom* that appear on the parent company's
official careers site.  Membership rule: only parent-board postings
explicitly attributed to Loom by

1. a structured field (legal employer, business unit, brand/team/organization)
   whose value names Loom;
2. or, where no such field exists, an explicit company name (``Loom``) in the
   job title;
3. or the role's own description header (the ``Your Future Team`` section)
   naming Loom as the hiring business/team.

Roles that merely mention the Loom product as a skill, tool, or suite member
are explicitly excluded.

Field audit (live 2026-11-08): the parent board's structured fields per posting
are title, category (= Atlassian function), locations, type, and iCIMS
apply/portal URLs; detail pages expose a header meta line
``department | locations | type``.  No legal-employer, business-unit, or
brand/team field exists (``team``/``employer`` are null for all 342 rows in
the verified Atlassian run), and there is no Loom facet/filter.  Rule 1
therefore yields nothing; rules 2-3 apply.  Rule 2 is enumerated over every
listing row (342 live rows).  Rule 3 is applied to every fetched detail.
The runner additionally audits rule 3 against the saved verified Atlassian
snapshot descriptions for the identical live ID set (see ``run.py``); any
live ID absent from that snapshot stops the run instead of being assumed
unattributed.

Under this rule the attributed set is exactly the two Loom-titled roles
26575 and 27431 (27431 is attributed by both rule 2 and rule 3: its
``Your Future Team`` section names "The Loom Integration Platform & Billing
Engineering team").  Both are collected with full descriptions.  If a
Loom-scoped filter/group ever appears, the adapter fails with
``unreviewed_company_scoped_listing_detected`` for a fresh review.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .atlassian import parse_detail_html, parse_listing_html
from .base import Adapter


OFFICIAL_ENTRY = "https://www.loom.com/careers"
OFFICIAL_ENTRY_REDIRECT = (
    "https://www.atlassian.com/company/careers/all-jobs?team=&location=&search=Loom"
)
PARENT_BOARD_HOST = "www.atlassian.com"
PARENT_BOARD_PATH = "/company/careers/all-jobs"
PARENT_BOARD_TITLE = "Atlassian Jobs: View Listings for Open Positions | Atlassian"
DETAIL_PATH = re.compile(r"^/company/careers/details/(\d+)$")
LOOM_TOKEN = re.compile(r"\bloom\b", re.I)
FUNCTION_LEGEND = "WHAT FUNCTION MATCHES YOU?"
LOCATION_LEGEND = "WHERE ARE YOU ELIGIBLE TO WORK?"
# The inspected parent board.  These are sanity bounds for the reviewed
# snapshot, not a collection cap; a smaller or Loom-only board is treated as a
# possible new company-scoped listing and is not auto-collected.
INSPECTED_PARENT_BOARD_JOBS = 342
INSPECTED_PARENT_BOARD_GROUPS = 17
INSPECTED_LOOM_BRANDED_IDS = ("26575", "27431")


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_official_entry(status: int, headers, content: bytes,
                         final_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the exact 308 Loom -> Atlassian parent-board redirect."""
    if final_url.rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
        raise ValueError(f"Loom official entry did not stay on the inspected URL: {final_url}")
    location = (headers.get("location") or "").strip() if hasattr(headers, "get") else ""
    if status != 308:
        raise ValueError(f"Loom official entry returned HTTP {status}, expected the inspected 308 redirect")
    if location != OFFICIAL_ENTRY_REDIRECT:
        raise ValueError(f"Loom official entry redirect target changed: {location!r}")
    return {
        "url": OFFICIAL_ENTRY,
        "status": status,
        "location": location,
        "refresh": headers.get("refresh") if hasattr(headers, "get") else None,
        "content_type": headers.get("content-type") if hasattr(headers, "get") else None,
        "response_bytes": len(content),
        "response_sha256": sha256(content).hexdigest(),
        "body_preview": content[:120].decode("utf-8", "replace"),
    }


def _filter_options(soup: BeautifulSoup) -> dict:
    options = {}
    for fieldset in soup.select("fieldset"):
        legend = fieldset.find("legend")
        legend_text = normalize_text(legend.get_text(" ", strip=True)) if legend else ""
        if not legend_text:
            continue
        labels = [normalize_text(label.get_text(" ", strip=True))
                  for label in fieldset.select("label")]
        labels = [label for label in labels if label]
        if FUNCTION_LEGEND in legend_text.upper():
            options["functions"] = labels
            options["function_legend"] = legend_text
        elif LOCATION_LEGEND in legend_text.upper():
            options["locations"] = labels
            options["location_legend"] = legend_text
    return options


def parse_parent_board(html: str, response_url: str = OFFICIAL_ENTRY_REDIRECT) -> dict:
    """Parse the exact Atlassian parent board exposed by Loom's redirect.

    The board is accepted as a *parent-wide* listing only when it shows the
    reviewed multi-function/multi-location structure and no Loom-scoped filter.
    The two Loom-branded rows are returned separately for evidence; they are
    never treated as the complete Loom membership.
    """
    parsed = urlsplit(str(response_url))
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.hostname != PARENT_BOARD_HOST
            or parsed.path.rstrip("/") != PARENT_BOARD_PATH):
        raise ValueError(f"Loom redirect landed outside the inspected Atlassian board: {response_url}")
    if query != {"team": [""], "location": [""], "search": ["Loom"]}:
        raise ValueError(f"Loom redirect query changed from the inspected shape: {query}")

    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != PARENT_BOARD_TITLE:
        raise ValueError(f"Atlassian parent board title changed: {title!r}")

    listing = parse_listing_html(html, response_url)
    rows = listing["rows"]
    groups = listing["groups"]
    advertised = listing["advertised_count"]
    if advertised is None:
        raise ValueError("Atlassian parent board no longer advertises a job count")
    if len(rows) != advertised:
        raise ValueError(
            f"Atlassian parent board rows {len(rows)} do not match advertised count {advertised}"
        )

    search_input = soup.select_one('input[placeholder="Search the results"]')
    if search_input is None:
        raise ValueError("Atlassian parent board search input is missing")
    search_value = search_input.get("value")
    filters = _filter_options(soup)
    if "functions" not in filters or "locations" not in filters:
        raise ValueError("Atlassian parent board function/location filters are missing")

    filter_text = " ".join(
        filters["functions"] + filters["locations"]
        + [filters.get("function_legend", ""), filters.get("location_legend", "")]
    ).lower()
    group_text = " ".join(group["department"] for group in groups).lower()
    loom_branded = [row for row in rows if LOOM_TOKEN.search(row["title"])]
    loom_scoped_filter = "loom" in filter_text or "loom" in group_text
    # The reviewed board is unambiguously parent-wide: many functions, many
    # locations, 342 rows, no Loom group/filter, and the Loom query is inert.
    company_scoped = bool(
        loom_scoped_filter
        or (advertised <= len(loom_branded) and len(loom_branded) > 0)
        or (len(groups) == 1 and loom_branded and groups[0]["department"].lower().find("loom") >= 0)
    )
    return {
        "url": response_url,
        "title": title,
        "advertised_count": advertised,
        "rows": rows,
        "row_count": len(rows),
        "groups": groups,
        "group_count": len(groups),
        "facet_counts": listing["facet_counts"],
        "duplicate_ids": listing["duplicate_ids"],
        "has_pager": listing["has_pager"],
        "pager_details": listing["pager_details"],
        "search_input_value": search_value,
        "search_param_applied_server_side": bool(search_value),
        "filters": filters,
        "function_filter_count": len(filters["functions"]),
        "location_filter_count": len(filters["locations"]),
        "loom_branded_rows": loom_branded,
        "loom_branded_count": len(loom_branded),
        "loom_scoped_filter": loom_scoped_filter,
        "company_scoped": company_scoped,
    }


STRUCTURED_ATTRIBUTION_FIELDS = (
    "legal_employer", "employer", "business_unit", "brand", "team", "organization", "organisation",
)
FUTURE_TEAM_HEADING = "your future team"
# Section boundaries that close a "Your Future Team" header in retained text.
FUTURE_TEAM_END = re.compile(
    r"(?:in this role, you will|your background|what you(?:'ll| will) do|"
    r"responsibilities|qualifications|minimum qualifications|compensation)",
    re.I,
)


def structured_attribution(fields: dict) -> list[str]:
    """Rule 1: structured legal-employer/business-unit/brand/team fields naming Loom.

    Returns the field names whose value explicitly names Loom.  The live
    Atlassian board/detail expose no such field (category is an Atlassian
    function; team/employer are null), so this returns [] there.
    """
    hits = []
    for name in STRUCTURED_ATTRIBUTION_FIELDS:
        value = fields.get(name)
        if value and LOOM_TOKEN.search(str(value)):
            hits.append(name)
    return hits


def future_team_header(text: str) -> str | None:
    """The role's own "Your Future Team" header text, or None if absent."""
    flat = normalize_text(text)
    match = re.search(re.escape(FUTURE_TEAM_HEADING), flat, re.I)
    if not match:
        return None
    rest = flat[match.end():]
    end = FUTURE_TEAM_END.search(rest)
    header = rest[:end.start()] if end else rest
    return header.strip() or None


def header_attribution(text: str) -> bool:
    """Rule 3: description header names Loom as the hiring business/team.

    Requires the first sentence of the "Your Future Team" header to name a
    Loom team/business (``Loom ... team`` — Loom modifying the hiring team).
    A product-suite list ("... ecosystem (Jira, ..., Loom, ...)"), a tool/skill
    mention, or a Loom mention outside the header does not qualify.
    """
    header = future_team_header(text)
    if not header:
        return False
    first_sentence = re.split(r"(?<=[.!?])\s", header, maxsplit=1)[0]
    return bool(re.search(
        r"\bLoom\b[^.!?()]*\b(?:team|business|organization|organisation|unit)\b",
        first_sentence, re.I,
    ))


def classify_posting(title: str, description_text: str = "", fields: dict | None = None) -> dict:
    """Apply the Round 3 user membership rule to one parent-board posting."""
    structured = structured_attribution(fields or {})
    title_hit = bool(LOOM_TOKEN.search(title or ""))
    header_hit = header_attribution(description_text or "")
    rules = []
    if structured:
        rules.append("structured_field:" + ",".join(structured))
    if title_hit:
        rules.append("title_names_loom")
    if header_hit:
        rules.append("description_header_names_loom_team")
    return {
        "attributed": bool(rules),
        "rules": rules,
        "structured_fields": structured,
        "title_names_loom": title_hit,
        "header_names_loom_team": header_hit,
        "future_team_header": future_team_header(description_text or ""),
    }


def scope_decision(board: dict) -> dict:
    """Explain the Round 3 membership decision for the parent board."""
    if board["company_scoped"]:
        return {
            "company_scoped": True,
            "reason": (
                "The Atlassian board now exposes a Loom-specific filter, group, or a Loom-only "
                "row set. This is a possible company-scoped listing and must be reviewed before "
                "any membership is collected."
            ),
            "parent_board_jobs": board["advertised_count"],
            "groups": board["group_count"],
            "loom_branded_rows": board["loom_branded_count"],
        }
    return {
        "company_scoped": False,
        "round3_user_scope_decision": "2026-11-08",
        "membership_rule": (
            "Only parent-board postings explicitly attributed to Loom by (1) a structured "
            "legal-employer/business-unit/brand/team/organization field naming Loom, or "
            "(2) an explicit 'Loom' in the job title, or (3) the role's own "
            "'Your Future Team' description header naming Loom as the hiring business/team. "
            "Roles merely mentioning the Loom product as a skill/tool are excluded."
        ),
        "structured_field_audit": (
            "Live parent board exposes only title, category (Atlassian function), locations, "
            "type, and iCIMS apply/portal URLs per posting; detail pages expose only "
            "'department | locations | type' header meta. No legal-employer/business-unit/"
            "brand/team field exists and no Loom facet/filter is offered, so rule 1 yields "
            "no postings and rules 2-3 decide membership."
        ),
        "reason": (
            "Loom's official careers URL redirects to Atlassian's parent-wide all-jobs board "
            "(342 jobs, 17 functions, 15 locations, no pager, inert search=Loom). Under the "
            "2026-11-08 user scope decision, Loom postings on the parent board are those "
            "explicitly attributed to Loom. Exactly 2 of 342 listing rows name Loom in the "
            "title (26575, 27431); 27431's 'Your Future Team' header also names the Loom "
            "team. No structured field attributes any posting to Loom. The other 340 rows "
            "are excluded, including 18 whose descriptions merely mention Loom as a product, "
            "tool, or suite member. Those 2 attributed postings are collected with full "
            "descriptions; the 342-row board itself is not Loom membership."
        ),
        "parent_board_jobs": board["advertised_count"],
        "groups": board["group_count"],
        "loom_branded_rows": board["loom_branded_count"],
        "attributed_jobs": board["loom_branded_count"],
        "search_param_applied_server_side": board["search_param_applied_server_side"],
        "filters": {
            "functions": board["filters"]["functions"],
            "locations": board["filters"]["locations"],
        },
        "inspected_snapshot": {
            "jobs": INSPECTED_PARENT_BOARD_JOBS,
            "groups": INSPECTED_PARENT_BOARD_GROUPS,
            "loom_branded_ids": list(INSPECTED_LOOM_BRANDED_IDS),
        },
    }


def parse_detail_page(html: str, expected_id: str, expected_title: str,
                      detail_url: str, *, status: int = 200) -> dict:
    """Parse one exact Loom-branded detail page from the parent board."""
    parsed_url = urlsplit(str(detail_url))
    match = DETAIL_PATH.fullmatch(parsed_url.path)
    if (parsed_url.scheme != "https" or parsed_url.hostname != PARENT_BOARD_HOST
            or match is None or match.group(1) != str(expected_id)):
        raise ValueError(f"Loom-branded detail URL is not the exact Atlassian detail route: {detail_url}")
    if status != 200:
        raise ValueError(f"Loom-branded detail {expected_id} returned HTTP {status}")
    soup = BeautifulSoup(html or "", "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = (canonical.get("href") or "").strip() if canonical else ""
    if canonical_url != detail_url:
        raise ValueError(
            f"Loom-branded detail {expected_id} canonical URL does not match: {canonical_url!r}"
        )
    parsed = parse_detail_html(html, expected_id, detail_url)
    title = normalize_text(parsed["title"])
    expected = normalize_text(expected_title)
    if title != expected:
        raise ValueError(f"Loom-branded detail {expected_id} title changed: {title!r} != {expected!r}")
    if not parsed["apply_urls"]:
        raise ValueError(f"Loom-branded detail {expected_id} publishes no iCIMS apply URL")
    if any("icims.com" not in url for url in parsed["apply_urls"]):
        raise ValueError(f"Loom-branded detail {expected_id} apply URL is not the observed iCIMS route")
    text = normalize_text(parsed["retained_text"])
    if len(text) < 100:
        raise ValueError(f"Loom-branded detail {expected_id} role body is empty or truncated")
    classification = classify_posting(title, text, {
        "legal_employer": None, "employer": None, "business_unit": None,
        "brand": None, "team": None, "organization": None,
    })
    if not classification["attributed"]:
        raise ValueError(
            f"Loom-branded detail {expected_id} is not attributed to Loom under the "
            "Round 3 rule (no structured field, title, or description header names Loom)"
        )
    return {
        "job_id": str(expected_id),
        "url": detail_url,
        "http_status": status,
        "title": title,
        "department": parsed["department"],
        "locations": parsed["locations"],
        "employment_type": parsed["employment_type"],
        "workplace": parsed.get("workplace"),
        "header_meta": parsed.get("header_meta"),
        "salary": parsed.get("salary"),
        "apply_url": parsed["apply_urls"][0] if len(parsed["apply_urls"]) == 1 else None,
        "apply_urls": parsed["apply_urls"],
        "description_html": parsed["retained_html"],
        "description_text": text,
        "description_chars": len(text),
        "description_sha256": sha256(text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(text),
        "loom_mentions_in_body": len(LOOM_TOKEN.findall(text)),
        "responsibilities": parsed["responsibilities"],
        "qualifications": parsed["qualifications"],
        "excluded_template_headings": parsed["excluded_template_headings"],
        "classification": classification,
    }


class Loom(Adapter):
    """Collect Loom-attributed postings from Atlassian's parent board (Round 3)."""

    VERIFICATION_REVISION = "loom.atlassian-parent-board.round3-attributed.1"
    DESCRIPTION_METHOD = "loom.atlassian_parent_board_attributed.detail_body_retained_sections_v1"

    async def _fetch_official_entry(self):
        started = time.perf_counter()
        request = self.http.client.build_request("GET", OFFICIAL_ENTRY)
        response = await self.http.client.send(request, follow_redirects=False)
        try:
            response._content = await response.aread()
            self.http._save(response, "GET", None)
        finally:
            await response.aclose()
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(
            response.status_code, response.headers, response.content, str(response.url)
        )
        parsed["seconds"] = seconds
        return parsed

    async def _fetch_parent_board(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY_REDIRECT)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_ENTRY_REDIRECT:
            raise ValueError(f"Loom redirect target redirected again: {response.url}")
        parsed = parse_parent_board(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _fetch_detail(self, row: dict):
        started = time.perf_counter()
        response = await self.http.get(row["url"])
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail_page(
            response.text, row["job_id"], row["title"], str(response.url),
            status=response.status_code,
        )
        parsed.update({
            "requested_url": row["url"],
            "final_url": str(response.url),
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "listing_department": row["department"],
            "listing_location": row["listing_location"],
        })
        return parsed

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "redirect_target": OFFICIAL_ENTRY_REDIRECT,
            "listing_url": OFFICIAL_ENTRY_REDIRECT,
            "listing_authority": (
                "Loom's own careers URL publishes a 308 redirect to Atlassian's parent-wide "
                "all-jobs page. Under the 2026-11-08 Round 3 user scope decision, membership "
                "is the subset of that board explicitly attributed to Loom (structured field, "
                "title, or description header). Every listing row was title-scanned; fetched "
                "details were header-checked."
            ),
            "detail_method": (
                "Each attributed posting was fetched from its exact Atlassian "
                "/company/careers/details/<ID> route and parsed with the same retained-section "
                "parser used for the verified Atlassian board; the Round 3 attribution rule "
                "was re-applied to the parsed title and retained body."
            ),
            "request_scope": (
                "Official Loom careers redirect, the exact Atlassian parent board it lands on, "
                "and the attributed detail pages only. No guessed ATS slug, aggregator, or "
                "keyword-search result was enumerated as Loom membership; non-attributed "
                "parent-board rows were not collected."
            ),
            "collection_policy": (
                "Only postings attributed to Loom under the Round 3 rule are added to "
                "jobs.jsonl. The full parent board is recorded as scope evidence."
            ),
        })

        try:
            self.page_guard()
            official = await self._fetch_official_entry()
        except Exception as exc:
            self.result.errors.append(f"loom_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["loom_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result
        self.result.metadata["official_entry"] = official

        try:
            self.page_guard()
            board = await self._fetch_parent_board()
        except Exception as exc:
            self.result.errors.append(f"loom_parent_board_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["loom_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        audit_started = time.perf_counter()
        decision = scope_decision(board)
        self.result.metadata["parent_board"] = {
            key: board[key] for key in (
                "url", "title", "advertised_count", "row_count", "group_count",
                "facet_counts", "duplicate_ids", "has_pager", "pager_details",
                "search_input_value", "search_param_applied_server_side",
                "function_filter_count", "location_filter_count",
                "loom_branded_count", "loom_scoped_filter", "company_scoped",
                "status", "seconds", "response_bytes", "response_sha256",
            )
        }
        self.result.metadata["filters"] = board["filters"]
        self.result.metadata["scope_decision"] = decision
        self.result.metadata["observed_loom_branded_rows"] = board["loom_branded_rows"]
        self.result.metadata["parent_board_job_ids"] = [
            row["job_id"] for row in board["rows"]
        ]
        self.result.timings["loom_scope_audit_seconds"] = max(
            0.0, time.perf_counter() - audit_started
        )

        if decision["company_scoped"]:
            self.result.errors.append("unreviewed_company_scoped_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_company_scoped_listing"] = {
                "advertised_count": board["advertised_count"],
                "groups": board["groups"],
                "filters": board["filters"],
                "search_input_value": board["search_input_value"],
                "loom_branded_rows": board["loom_branded_rows"],
            }
            self.result.timings["loom_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        detail_started = time.perf_counter()
        attributed_rows = board["loom_branded_rows"]
        observed = []
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))

        async def one(row):
            async with semaphore:
                if not self.page_guard():
                    return {
                        "job_id": row["job_id"],
                        "url": row["url"],
                        "title": row["title"],
                        "error": "loom_detail_page_budget_exhausted",
                    }
                try:
                    return await self._fetch_detail(row)
                except Exception as exc:
                    return {
                        "job_id": row["job_id"],
                        "url": row["url"],
                        "title": row["title"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        if attributed_rows:
            observed = list(await asyncio.gather(*(one(row) for row in attributed_rows)))
        self.result.timings["loom_detail_seconds"] = max(
            0.0, time.perf_counter() - detail_started
        )

        detail_errors = [item for item in observed if item.get("error")]
        for item in detail_errors:
            self.result.errors.append(
                f"loom_attributed_detail_failed:{item['job_id']}: {item['error']}"
            )
        self.result.metadata["observed_loom_branded_postings"] = observed
        self.result.metadata["observed_loom_branded_count"] = len(
            [item for item in observed if not item.get("error")]
        )
        self.result.metadata["observed_loom_branded_ids"] = [
            item["job_id"] for item in observed if not item.get("error")
        ]

        # Collect every attributed posting with its full retained description.
        for item in observed:
            if item.get("error"):
                continue
            raw = {
                "listing": {
                    "job_id": item["job_id"], "title": item["title"], "url": item["url"],
                    "department": item.get("listing_department"),
                    "listing_location": item.get("listing_location"),
                },
                "detail_header_meta": item.get("header_meta"),
                "excluded_template_headings": item.get("excluded_template_headings"),
                "apply_urls": item.get("apply_urls"),
                "round3_classification": item.get("classification"),
                "round3_membership_rule": (
                    "2026-11-08 user scope decision: parent-board postings explicitly "
                    "attributed to Loom by structured field, title, or description header."
                ),
            }
            job = self.job(
                raw,
                title=item["title"],
                url=item["url"],
                job_id=item["job_id"],
                apply_url=item.get("apply_url"),
                description=item.get("description_html") or "",
                method=self.DESCRIPTION_METHOD,
                description_url=item["url"],
                locations=item.get("locations") or [],
                department=item.get("department"),
                employment_type=item.get("employment_type"),
                workplace_type=item.get("workplace"),
                salary=item.get("salary"),
                responsibilities=item.get("responsibilities"),
                qualifications=item.get("qualifications"),
            )
            job.description_text = item.get("description_text") or ""
            job.record_identity = f"loom:{item['job_id']}"
            if len(item.get("apply_urls") or []) != 1:
                job.warnings.append("multiple_company_apply_links_retained_in_raw")
            if not job.description_text.strip():
                self.result.errors.append(f"{item['job_id']}: retained description is empty")
            self.add(job)

        collected = [
            item for item in observed if not item.get("error")
        ]
        self.result.expected_total = len(attributed_rows)
        self.result.listing_complete = bool(collected) and not detail_errors
        self.result.metadata["job_ids"] = [item["job_id"] for item in collected]
        self.result.metadata["listing_state"] = (
            "round3_attributed_membership_collected" if collected
            else "round3_attributed_membership_failed"
        )
        self.result.metadata["attributed_postings"] = collected
        self.result.metadata["scope_decision"]["observed_loom_branded_ids"] = [
            item["job_id"] for item in collected
        ]
        self.result.timings["loom_source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
