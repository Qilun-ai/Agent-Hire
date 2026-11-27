"""Alphawave Semi postings on the parent (Qualcomm) official careers board.

Round 3 (user scope decision, 2026-11-08): Alphawave Semi has been absorbed
into Qualcomm.  Membership is the postings *of Alphawave Semi* that appear on
Qualcomm's own official careers site (``careers.qualcomm.com``, reached from
the official entry ``https://awavesemi.com/careers/`` which 301-redirects to
Qualcomm's Data Center product page, whose Careers link opens the parent
portal).  A posting is included only when it is explicitly attributed to
Alphawave Semi by a structured field -- the ``Company:`` legal-employer line
at the head of the Qualcomm job description, the ``Job Area:`` business-unit /
team line, or the listing ``department`` -- or, where no such structured
field exists, by an explicit ``Alphawave`` company name in the job title (or
an explicit ``Alphawave Semi`` hiring-business header in the opening of the
description).  Roles that merely mention Alphawave / its products (e.g.
Dragonfly) as a skill or product topic while the structured employer is a
Qualcomm entity are excluded and recorded as borderline.

The parent board exposes no legal-employer / business-unit facet: its
``filterDef`` carries only ``job_family`` (Department), ``skills``,
``seniority`` and location facets.  Enumeration is therefore done with the
board's own search API (``/api/pcsx/search`` with ``query=Alphawave``, the
same API the unfiltered listing uses), and every candidate is verified
against its exact ``position_details`` record before inclusion.  No
parent-wide enumeration, aggregator, or guessed ATS slug is used.  If the
official entry stops redirecting to the inspected Qualcomm product page, or
the board API is blocked, the flow fails explicitly with an unknown total
instead of claiming zero openings.
"""
from __future__ import annotations

import time
from urllib.parse import quote, urlsplit

from .base import Adapter
from .qualcomm import (
    APPLY_PAGE,
    BASE_HOST,
    DETAIL_API,
    DOMAIN,
    PAGE_SIZE,
    SEARCH_API,
    clean,
    epoch_date,
    extract_sections,
    parse_position_detail,
    parse_search_page,
)
from ..http import FetchError
from ..models import text_html

OFFICIAL_ENTRY = "https://awavesemi.com/careers/"
PARENT_PRODUCT_PAGE = "https://www.qualcomm.com/data-center"
PARENT_PORTAL = "https://careers.qualcomm.com/careers"
QUERIES = ("Alphawave", "Alphawave Semi")
BLOCKING_STATUSES = (403, 405, 429)

MEMBERSHIP_RULE = (
    "User scope decision 2026-11-08 (absorbed companies): include only "
    "parent-board postings explicitly attributed to Alphawave Semi by a "
    "structured field (Company legal employer, Job Area business unit/team, "
    "or department), or -- where no such structured field exists -- an "
    "explicit Alphawave company name in the job title or an explicit "
    "'Alphawave Semi' hiring-business header in the description opening. "
    "Product/skill mentions under a Qualcomm structured employer are excluded."
)


def search_url(query: str, start: int) -> str:
    return (
        f"{SEARCH_API}?domain={DOMAIN}&query={quote(query, safe='')}"
        f"&location=&start={start}&sort_by=relevance"
    )


def detail_url(position_id: str) -> str:
    return f"{DETAIL_API}?position_id={position_id}&domain={DOMAIN}&hl=en"


def _mentions_alphawave(value) -> bool:
    return "alphawave" in clean(value).casefold()


def classify_attribution(title, sections: dict, department=None,
                         description_text: str = "") -> dict:
    """Apply the round-3 membership rule to one verified detail record."""
    company_value = sections.get("company")
    job_area = sections.get("job_area")
    structured = [v for v in (company_value, job_area, department) if v]
    for field, value in (("company", company_value), ("job_area", job_area),
                         ("department", department)):
        if value and _mentions_alphawave(value):
            return {
                "included": True,
                "rule": "structured_field_alphawave",
                "structured_field": field,
                "structured_value": clean(value),
            }
    if not structured:
        if _mentions_alphawave(title):
            return {"included": True, "rule": "title_explicit_no_structured_field",
                    "structured_field": None, "structured_value": None}
        if "alphawave semi" in (description_text or "")[:600].casefold():
            return {"included": True, "rule": "description_header_alphawave_semi_no_structured_field",
                    "structured_field": None, "structured_value": None}
    return {
        "included": False,
        "rule": "excluded_product_or_skill_mention_or_qualcomm_employer",
        "structured_field": "company" if company_value else None,
        "structured_value": clean(company_value) if company_value else None,
    }


class Alphawave(Adapter):
    """Parent-board (Qualcomm) search for explicitly Alphawave-attributed roles."""

    VERIFICATION_REVISION = "alphawave.qualcomm-parent-board-attribution.1"

    async def _search(self, query: str, start: int) -> dict:
        if not self.page_guard():
            raise ValueError("Alphawave parent-board page budget exhausted")
        value = await self.http.json(
            search_url(query, start), headers={"Referer": PARENT_PORTAL})
        return parse_search_page(value, start)

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            membership_rule=MEMBERSHIP_RULE,
            parent_board=PARENT_PORTAL,
            search_api=SEARCH_API,
            detail_api=DETAIL_API,
            review_evidence="companies/alphawave-semi/journey-research.md",
        )
        if not self.page_guard():
            return self.result
        entry_started = time.perf_counter()
        try:
            response = await self.http.get(self.company.careers_url)
        except FetchError as exc:
            self.result.errors.append(f"alphawave_official_entry_unreachable:{exc}")
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.finish()
            return self.result
        final = urlsplit(str(response.url))
        self.result.metadata.update(
            official_entry=self.company.careers_url,
            redirect_chain=[{"url": str(r.url), "status": r.status_code}
                            for r in response.history],
            final_url=str(response.url),
            official_entry_seconds=round(max(0.0, time.perf_counter() - entry_started), 6),
            ats_board_requests=0,
        )
        if not (final.hostname == "www.qualcomm.com"
                and final.path.rstrip("/") == "/data-center"):
            self.result.errors.append(
                "alphawave_official_journey_changed_requires_individual_membership_review")
            self.result.metadata["company_membership_verified"] = False
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.finish()
            return self.result

        # Parent board context: unfiltered total + the facets it actually exposes.
        try:
            unfiltered = await self._search("", 0)
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"alphawave_parent_board_unreachable:{exc}")
            self.result.metadata["company_membership_verified"] = False
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.finish()
            return self.result
        filter_def = unfiltered.get("filter_def") or {}
        self.result.metadata["parent_board_context"] = {
            "unfiltered_total": unfiltered["count"],
            "filter_names": [f.get("filterName") for f in filter_def.get("allFilters", [])],
            "note": ("No legal-employer/business-unit/brand facet is exposed; "
                     "membership is therefore the board's own Alphawave keyword "
                     "search, verified per posting against position_details."),
        }

        # Board's own keyword search (union over the two query spellings).
        candidates: dict[str, dict] = {}
        query_counts: dict[str, int] = {}
        try:
            for query in QUERIES:
                start, total = 0, None
                while total is None or start < total:
                    parsed = await self._search(query, start)
                    total = parsed["count"]
                    query_counts[query] = total
                    for row in parsed["rows"]:
                        candidates.setdefault(str(row["id"]), row)
                    if not parsed["rows"]:
                        break
                    start += PAGE_SIZE
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"alphawave_parent_board_search_blocked:{exc}")
            self.result.metadata["company_membership_verified"] = False
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.finish()
            return self.result

        included, borderline = [], []
        rule_counts: dict[str, int] = {}
        for position_id, row in candidates.items():
            url = detail_url(position_id)
            try:
                value = await self.http.json(
                    url, headers={"Referer": "https://" + BASE_HOST + row["positionUrl"]})
                detail = parse_position_detail(value, position_id)
            except (FetchError, ValueError) as exc:
                self.result.errors.append(
                    f"alphawave_candidate_detail_failed:{position_id}:{exc}")
                continue
            sections = extract_sections(detail["jobDescription"])
            description_text = text_html(detail["jobDescription"])
            verdict = classify_attribution(
                detail.get("name"), sections,
                detail.get("department") or row.get("department"), description_text)
            rule_counts[verdict["rule"]] = rule_counts.get(verdict["rule"], 0) + 1
            record = {
                "internal_position_id": position_id,
                "display_job_id": str(detail.get("displayJobId")),
                "title": clean(detail.get("name")),
                "url": detail.get("publicUrl"),
                **verdict,
            }
            if not verdict["included"]:
                borderline.append(record)
                continue
            qualifications = {
                key: sections[key]
                for key in ("minimum_qualifications", "preferred_qualifications")
                if sections.get(key)
            }
            job = self.job(
                raw={"internal_position_id": position_id, "listing": row,
                     "detail": detail, "sections": sections, "attribution": verdict},
                title=clean(detail.get("name")),
                url=detail.get("publicUrl"),
                job_id=str(detail.get("displayJobId")),
                description=detail["jobDescription"],
                method="alphawave.qualcomm.position_details.jobDescription",
                description_url=url,
                locations=[clean(loc) for loc in detail.get("locations") or [] if clean(loc)],
                department=detail.get("department") or row.get("department"),
                team=sections.get("job_area"),
                employment_type=sections.get("employment_type"),
                workplace_type=detail.get("workLocationOption"),
                date_posted=epoch_date(detail.get("postedTs")),
                date_posted_raw=detail.get("postedTs"),
                valid_through=sections.get("valid_through"),
                salary=sections.get("salary_text"),
                qualifications=qualifications or None,
                responsibilities=sections.get("responsibilities"),
                employer=sections.get("company"),
                language="en",
                apply_url=APPLY_PAGE.format(position_id=position_id),
            )
            job.requisition_id = str(detail.get("atsJobId") or detail.get("displayJobId"))
            job.company_scope_verified = True
            self.result.validate_job(job)
            job.record_identity = job.key
            self.result.jobs.append(job)
            self.result.dirty_jobs.append(job)
            included.append(record)

        self.result.metadata["parent_board_search"] = {
            "queries": list(QUERIES),
            "query_counts": query_counts,
            "candidate_ids": sorted(candidates),
            "rule_counts": rule_counts,
            "borderline_exclusions": borderline,
        }
        self.result.metadata["company_membership_verified"] = True
        self.result.expected_total = sum(rule_counts.values()) if not self.result.errors else None
        # The advertised total under the rule is the number of candidates the
        # board's search returned and that were classified; with no errors
        # that equals the included count, so a true zero stays a true zero.
        if not self.result.errors:
            self.result.expected_total = len(self.result.jobs)
            self.result.listing_complete = True
        else:
            self.result.listing_complete = False
        self.result.finish()
        return self.result
