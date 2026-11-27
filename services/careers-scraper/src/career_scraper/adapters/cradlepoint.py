"""Round-3 absorbed-company gate: full parent-board enumeration under the literal rule.

Official ``https://cradlepoint.com/about-us/careers/`` redirects through
``cradlepoint.com/about/careers`` to the Ericsson Enterprise Wireless Solutions
careers page (``cradlepoint.ericsson.com/about/careers/``). That page exposes
culture/values copy and a single generic ``Job opportunities`` / ``Find Jobs``
link to the parent Ericsson Eightfold board (``domain=ericsson.com``, title
``Careers at Ericsson``) with no Cradlepoint filter, subdivision, company or
division facet, or exact per-job links.

Round-3 user scope decision (2026-11-08): because Cradlepoint was absorbed
into Ericsson, collect only parent-board postings explicitly attributed to
this company by a structured field (legal employer, business unit,
brand/team/organization) or, where no such field exists, by an explicit
company name in the job title or the role's own description header naming it
as the hiring business; roles that merely mention the product as a skill are
excluded. Enumerate the parent board only as far as needed to apply the rule.

This adapter therefore:

* rechecks the redirect chain, exact published CTA and hosted-board identity;
* enumerates the whole parent search listing (every ``start`` page at 10 rows)
  to inventory preview/detail fields and scan every title;
* probes the board's own search API with ``query=cradlepoint`` (0),
  ``query=Enterprise Wireless`` and ``query=BEWS`` as candidate generators only;
* fetches full detail bodies for every ``Enterprise Wireless`` / ``BEWS``
  candidate and applies the header rule with literal ``Cradlepoint`` (not
  keyword or entity inference).

Live result (2026-11-08 exploration): 491 parent rows, 0 titles containing
``Cradlepoint``, 0 detail bodies containing ``Cradlepoint``, no structured
company/division field (filters are only ``skills``/``function``/``jobtype``,
facets only ``locations``; preview/detail keys carry only department and
function). Fourteen candidate bodies contain ``Enterprise Wireless`` and
eleven of them name it as the hiring business in the header, but none names
``Cradlepoint``; under the literal rule they are borderline exclusions (the
official ``llms.txt`` states Cradlepoint and Ericsson Enterprise Wireless
Solutions are the same corporate entity, but the rule requires the explicit
company name ``Cradlepoint``). The adapter therefore stores zero jobs and
preserves an unknown company total. No universal ATS discovery and no
keyword/product membership inference are used.
"""
import hashlib
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import text_html
from .base import Adapter


def _mentions(text):
    return len(re.findall(r"cradlepoint", text or "", re.I))


def _eews_mentions(text):
    return len(re.findall(r"enterprise wireless", text or "", re.I))


def _bews_mentions(text):
    return len(re.findall(r"\bBEWS\b", text or ""))


def compact_body_hash(html):
    """Mirror the browser-reference NFC/whitespace-free body hash."""
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html or ""))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_seconds(value):
    return round(max(0.0, float(value)), 6)


class Cradlepoint(Adapter):
    """Follow only Cradlepoint's official journey; apply the literal round-3 rule."""

    VERIFICATION_REVISION = "cradlepoint.round3-parent-ericsson-full-enumeration.2"
    ENTRY = "https://cradlepoint.com/about-us/careers/"
    LANDING = "https://cradlepoint.ericsson.com/about/careers/"
    FIND_JOBS = "https://jobs.ericsson.com/careers?page=1&jobPipeline=careersite&query="
    SEARCH_BASE = "https://jobs.ericsson.com/api/pcsx/search"
    SEARCH_API = SEARCH_BASE + "?domain=ericsson.com&query=&location=&start=0&"
    DETAIL_API = "https://jobs.ericsson.com/api/pcsx/position_details?position_id={position_id}&domain=ericsson.com"
    CHANGED = "cradlepoint_official_journey_changed_requires_individual_membership_review"
    SCOPE_ERROR = (
        "cradlepoint_official_journey_redirects_to_ericsson_enterprises_wireless_careers_page"
        "_linking_to_parent_ericsson_board_without_cradlepoint_membership_round3_full_enumeration_zero_literal"
    )
    OFFICIAL_HOSTS = {"cradlepoint.com", "www.cradlepoint.com", "cradlepoint.ericsson.com"}
    HOSTED_HOST = "jobs.ericsson.com"
    PAGE_SIZE = 10
    HEADER_CHARS = 2000
    # Header hiring-business patterns for the Ericsson Enterprise Wireless
    # Solutions entity. These classify borderline candidates only; a positive
    # match still does NOT satisfy the literal ``Cradlepoint`` rule.
    # "About" is matched case-sensitively (capital A as a section header) so
    # that "Learn more about Ericsson Enterprise Wireless Solutions" (lowercase,
    # mid-sentence CTA) does not count as a hiring-business header.
    EEWS_HIRING_PATTERNS = (
        r"Ericsson\s+Enterprise\s+Wireless\s+Solutions\s+Inc\.?\s+does\s+not\s+sponsor",
        r"Ericsson\s+Enterprise\s+Wireless\s+Solutions\s+is\s+(seeking|expanding|building)",
        r"As\s+an\s+SDR\s+at\s+Ericsson\s+Enterprise\s+Wireless",
        r"(?<!more )About\s+Ericsson\s+Enterprise\s+Wireless\s+Solutions",
        r"Ericsson\s+Enterprise\s+Wireless\s+Solutions\s+\(BEWS\)\s+is\s+the\s+group",
        r"Within\s+Enterprise\s+Wireless\s+Solutions\s+we\s+are\s+building",
        r"expanding\s+Ericsson\s+Enterprise\s+Wireless\s+Solutions['\u2019]?\s+presence",
        r"expansion\s+of\s+Ericsson\s+Enterprise\s+Wireless\s+Solutions?\W?s?\s+presence",
    )
    PREVIEW_KEYS = {
        "id", "displayJobId", "name", "locations", "standardizedLocations",
        "postedTs", "solrScore", "stars", "department", "creationTs", "isHot",
        "workLocationOption", "locationFlexibility", "atsJobId", "positionUrl",
        "efcustomTextJobfuntionAdvancefilterpcs",
    }
    DETAIL_KEYS = {
        "id", "displayJobId", "name", "locations", "standardizedLocations",
        "postedTs", "stars", "department", "creationTs", "jobDescription",
        "location", "isHot", "workLocationOption", "locationFlexibility",
        "atsJobId", "positionUrl", "publicUrl", "jdHighlight",
        "positionExtraDetails", "positionUserActions",
    }
    EXPECTED_FILTERS = {"skills", "function", "jobtype"}
    EXPECTED_FACETS = {"locations"}
    COMPANY_FIELD_SUBSTRINGS = (
        "company", "employer", "business", "brand", "organisation",
        "organization", "division", "unit", "legalentity", "legal_entity",
    )

    @staticmethod
    def _search_url(query, start, location=""):
        return (
            Cradlepoint.SEARCH_BASE
            + "?domain=ericsson.com&query="
            + quote(str(query), safe="")
            + "&location="
            + quote(str(location), safe="")
            + f"&start={int(start)}&"
        )

    @staticmethod
    def _official_entry_ok(url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in Cradlepoint.OFFICIAL_HOSTS:
            return False
        return parsed.path.rstrip("/") in {"/about-us/careers", "/about/careers"}

    @staticmethod
    def _find_jobs_links(html, base_url):
        soup = BeautifulSoup(html, "lxml")
        resolved = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:")):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlsplit(absolute)
            if parsed.hostname == Cradlepoint.HOSTED_HOST and parsed.path.rstrip("/") == "/careers":
                resolved.append(absolute)
        return sorted(set(resolved))

    @staticmethod
    def _internal_job_links(html, base_url):
        soup = BeautifulSoup(html, "lxml")
        found = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            if not href:
                continue
            absolute = urljoin(base_url, href)
            parsed = urlsplit(absolute)
            if parsed.hostname in Cradlepoint.OFFICIAL_HOSTS and (
                "/careers/job/" in parsed.path or parsed.path.rstrip("/").endswith("/careers/job")
            ):
                found.append(absolute)
        return sorted(set(found))

    @staticmethod
    def _search_probe(payload, raw_text):
        if not isinstance(payload, dict):
            raise ValueError("Cradlepoint scope probe search response is not an object")
        data = payload.get("data") or {}
        positions = data.get("positions") or []
        filter_def = data.get("filterDef") or {}
        facets = filter_def.get("facets") or {}
        locations = facets.get("locations") or []
        return {
            "url": Cradlepoint.SEARCH_API,
            "domain": "ericsson.com",
            "query": "",
            "parent_total": data.get("count"),
            "preview_count": len(positions),
            "preview_job_ids": [str(p.get("id")) for p in positions if isinstance(p, dict)],
            "preview_display_job_ids": [str(p.get("displayJobId")) for p in positions if isinstance(p, dict)],
            "preview_departments": sorted({str(p.get("department")) for p in positions if isinstance(p, dict) and p.get("department")}),
            "facet_locations": [list(pair) for pair in locations],
            "cradlepoint_mentions": _mentions(raw_text),
            "applied_filters": data.get("appliedFilters"),
        }

    @staticmethod
    def _preview_page(payload, raw_text, query="", start=0):
        if not isinstance(payload, dict):
            raise ValueError("Cradlepoint search page is not an object")
        data = payload.get("data") or {}
        positions = data.get("positions") or []
        filter_def = data.get("filterDef") or {}
        facets = filter_def.get("facets") or {}
        all_filters = filter_def.get("allFilters") or []
        previews = []
        for row in positions:
            if not isinstance(row, dict):
                continue
            previews.append({
                "id": str(row.get("id")),
                "display_job_id": str(row.get("displayJobId")),
                "title": row.get("name"),
                "department": row.get("department"),
                "locations": row.get("locations"),
                "function": row.get("efcustomTextJobfuntionAdvancefilterpcs"),
            })
        return {
            "url": Cradlepoint._search_url(query, start),
            "query": query,
            "start": start,
            "parent_total": data.get("count"),
            "preview_count": len(previews),
            "previews": previews,
            "preview_job_ids": [p["id"] for p in previews],
            "position_keys": sorted(previews and (positions[0].keys() if isinstance(positions[0], dict) else []) or []),
            "data_keys": sorted(data.keys()),
            "filter_names": sorted({str(a.get("filterName")) for a in all_filters if isinstance(a, dict) and a.get("filterName")}),
            "facet_keys": sorted(facets.keys()),
            "departments": sorted({str(p.get("department")) for p in previews if p.get("department")}),
            "functions": sorted({str(v) for p in previews for v in (p.get("function") or [])}),
            "cradlepoint_mentions": _mentions(raw_text),
            "title_cradlepoint_ids": [p["id"] for p in previews if re.search(r"\bcradlepoint\b", p.get("title") or "", re.I)],
            "title_enterprise_wireless_ids": [p["id"] for p in previews if re.search(r"enterprise wireless", p.get("title") or "", re.I)],
            "title_netcloud_ids": [p["id"] for p in previews if re.search(r"netcloud", p.get("title") or "", re.I)],
        }

    @staticmethod
    def _classify_candidate(description_html):
        """Apply the literal rule and the EEWS-entity borderline taxonomy."""
        text = text_html(description_html or "")
        header = text[: Cradlepoint.HEADER_CHARS]
        has_cp_header = bool(re.search(r"\bcradlepoint\b", header, re.I))
        has_cp_any = _mentions(text) > 0
        eews_header = _eews_mentions(header) > 0 or _bews_mentions(header) > 0
        eews_any = _eews_mentions(text) > 0 or _bews_mentions(text) > 0
        hiring = None
        for pattern in Cradlepoint.EEWS_HIRING_PATTERNS:
            match = re.search(pattern, header, re.I)
            if match:
                hiring = match.group(0)
                break
        if has_cp_header:
            category = "literal_cradlepoint_header_hit"
            reason = "header_names_cradlepoint"
        elif eews_header and hiring:
            category = "eews_entity_hiring_business_header_observed"
            reason = "eews_hiring_header_without_cradlepoint_name_excluded_under_literal_rule"
        elif eews_header:
            category = "excluded_portfolio_or_product_mention_in_header"
            reason = "eews_portfolio_or_product_mention_not_hiring_business"
        elif eews_any:
            category = "excluded_benefits_or_later_mention_only"
            reason = "eews_only_after_header_benefits_or_trailer"
        else:
            category = "excluded_token_only"
            reason = "no_eews_phrase_in_body_tokens_matched_separately"
        return {
            "header_chars": len(header),
            "header_text_head": header[:600],
            "cradlepoint_in_header": has_cp_header,
            "cradlepoint_in_description": has_cp_any,
            "eews_in_header": eews_header,
            "eews_in_description": eews_any,
            "eews_hiring_pattern": hiring,
            "category": category,
            "reason": reason,
        }

    @staticmethod
    def _detail_probe(payload, raw_text, position_id):
        if not isinstance(payload, dict):
            raise ValueError("Cradlepoint scope probe detail response is not an object")
        data = payload.get("data") or {}
        description = data.get("jobDescription") or ""
        reference = {
            "url": Cradlepoint.DETAIL_API.format(position_id=position_id),
            "position_id": str(data.get("id")),
            "display_job_id": str(data.get("displayJobId")),
            "title": data.get("name"),
            "department": data.get("department"),
            "locations": data.get("locations"),
            "standardized_locations": data.get("standardizedLocations"),
            "posted_ts": data.get("postedTs"),
            "creation_ts": data.get("creationTs"),
            "description_chars": len(text_html(description)),
            "description_compact_sha256": compact_body_hash(description),
            "cradlepoint_mentions": _mentions(raw_text),
            "cradlepoint_mentions_in_description": _mentions(text_html(description)),
            "data_keys": sorted(data.keys()),
            "company_scope": "Ericsson research only; not a Cradlepoint vacancy",
        }
        reference.update(Cradlepoint._classify_candidate(description))
        return reference

    def _changed(self):
        self.result.errors.append(self.CHANGED)
        return self.result

    async def run(self):
        started = time.perf_counter()
        full_started = None
        cand_started = None
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=self.company.careers_url,
            company_membership_verified=False,
            parent_jobs_collected=0,
            ats_board_requests=0,
            detail_requests=0,
            scope_probe_requests=0,
            review_evidence="companies/cradlepoint/manual-reference.json",
            scope_observations=[],
            enumeration_pages=[],
            enumerated_ids=[],
            enumerated_previews=[],
            query_observations=[],
            candidate_detail_references=[],
            position_detail_references=[],
            counts_by_rule={},
            membership_rule=(
                "Round-3 user scope decision 2026-11-08: only parent-board postings explicitly "
                "attributed to Cradlepoint by a structured field (legal employer, business unit, "
                "brand/team/organization) or, where no such field exists, by an explicit "
                "'Cradlepoint' in the job title or the role's own description header naming it "
                "as the hiring business (header = first 2000 chars of text_html); product/skill "
                "mentions excluded. EEWS-entity headers observed but excluded as non-literal."
            ),
            membership_authority=(
                "Cradlepoint official entry https://cradlepoint.com/about-us/careers/ "
                "redirects to Ericsson Enterprise Wireless Solutions careers page; "
                "its single Find Jobs link targets the parent Ericsson Eightfold board "
                "(domain ericsson.com) enumerated in full under the literal round-3 rule"
            ),
        )
        # Unknown membership never represents an authoritative zero-job listing.
        self.result.expected_total = None
        self.result.listing_complete = False
        try:
            if not self.page_guard():
                return self.result
            entry = await self.http.get(self.company.careers_url)
            entry_url = str(entry.url)
            self.result.metadata["scope_probe_requests"] += 1
            self.result.metadata.update(
                official_entry_requested=self.company.careers_url,
                final_entry_url=entry_url,
                redirect_chain=[
                    {"url": str(hop.url), "status": hop.status_code} for hop in entry.history
                ],
            )
            if not self._official_entry_ok(entry_url):
                return self._changed()
            if entry_url.rstrip("/") != self.LANDING.rstrip("/"):
                self.result.metadata["observed_landing"] = entry_url
            entry_text = entry.text
            if _mentions(entry_text) == 0 and "Careers" not in entry_text:
                return self._changed()
            find_jobs = self._find_jobs_links(entry_text, entry_url)
            internal_jobs = self._internal_job_links(entry_text, entry_url)
            self.result.metadata.update(
                find_jobs_links_on_official_page=find_jobs,
                internal_exact_job_links_on_official_page=internal_jobs,
            )
            if find_jobs != [self.FIND_JOBS]:
                return self._changed()
            if internal_jobs:
                return self._changed()

            if not self.page_guard():
                return self.result
            hosted = await self.http.get(self.FIND_JOBS)
            self.result.metadata["scope_probe_requests"] += 1
            hosted_url = str(hosted.url)
            soup = BeautifulSoup(hosted.text, "lxml")
            title = (soup.title.get_text(strip=True) if soup.title else "")
            hosted_text = hosted.text
            self.result.metadata.update(
                hosted_board_url=hosted_url,
                hosted_board_title=title,
                hosted_domain=urlsplit(hosted_url).hostname,
                hosted_board_cradlepoint_mentions=_mentions(hosted_text),
            )
            if (
                urlsplit(hosted_url).hostname != self.HOSTED_HOST
                or title != "Careers at Ericsson"
                or "ericsson.com" not in hosted_text
            ):
                return self._changed()

            # Full parent-board enumeration, 10 rows per page.
            full_started = time.perf_counter()
            parent_total = None
            seen_ids = set()
            ordered_ids = []
            previews_all = []
            first_keys = None
            all_data_keys = set()
            all_filter_names = set()
            all_facet_keys = set()
            all_departments = set()
            all_functions = set()
            title_cp, title_ew, title_nc = [], [], []
            start = 0
            while True:
                if not self.page_guard():
                    return self.result
                page_url = self._search_url("", start)
                resp = await self.http.get(page_url)
                self.result.metadata["scope_probe_requests"] += 1
                if str(resp.url) != page_url:
                    return self._changed()
                try:
                    payload = resp.json()
                except ValueError:
                    return self._changed()
                try:
                    page = self._preview_page(payload, resp.text, "", start)
                except ValueError:
                    return self._changed()
                total = page["parent_total"]
                if not isinstance(total, int) or total <= 0:
                    return self._changed()
                if parent_total is None:
                    parent_total = total
                    first_keys = set(page["position_keys"])
                    if first_keys != self.PREVIEW_KEYS:
                        return self._changed()
                    if set(page["filter_names"]) != self.EXPECTED_FILTERS:
                        return self._changed()
                    if set(page["facet_keys"]) != self.EXPECTED_FACETS:
                        return self._changed()
                elif total != parent_total:
                    return self._changed()
                if set(page["position_keys"]) != self.PREVIEW_KEYS:
                    return self._changed()
                if set(page["filter_names"]) != self.EXPECTED_FILTERS:
                    return self._changed()
                if set(page["facet_keys"]) != self.EXPECTED_FACETS:
                    return self._changed()
                if page["cradlepoint_mentions"] != 0:
                    return self._changed()
                if page["preview_count"] == 0:
                    break
                # Last page must carry exactly the remainder.
                expected_here = min(self.PAGE_SIZE, parent_total - start)
                if page["preview_count"] != expected_here:
                    return self._changed()
                for preview in page["previews"]:
                    pid = preview["id"]
                    if pid in seen_ids:
                        return self._changed()
                    seen_ids.add(pid)
                    ordered_ids.append(pid)
                previews_all.extend(page["previews"])
                all_data_keys.update(page["data_keys"])
                all_filter_names.update(page["filter_names"])
                all_facet_keys.update(page["facet_keys"])
                all_departments.update(page["departments"])
                all_functions.update(page["functions"])
                title_cp.extend(page["title_cradlepoint_ids"])
                title_ew.extend(page["title_enterprise_wireless_ids"])
                title_nc.extend(page["title_netcloud_ids"])
                self.result.metadata["enumeration_pages"].append({
                    "start": start,
                    "url": page["url"],
                    "parent_total": total,
                    "preview_count": page["preview_count"],
                    "preview_job_ids": page["preview_job_ids"],
                })
                if len(seen_ids) >= parent_total:
                    break
                start += page["preview_count"]
            self.result.timings["full_enumeration_seconds"] = _now_seconds(
                time.perf_counter() - full_started
            )
            if len(seen_ids) != parent_total or title_cp:
                return self._changed()
            if parent_total is not None and len(seen_ids) != parent_total:
                return self._changed()
            self.result.metadata.update(
                parent_board_count_observed=parent_total,
                enumeration_page_count=len(self.result.metadata["enumeration_pages"]),
                enumeration_preview_total=len(seen_ids),
                enumerated_ids=ordered_ids,
                enumerated_previews=previews_all,
                enumerated_departments=sorted(all_departments),
                enumerated_functions=sorted(all_functions),
                preview_keys=sorted(first_keys or []),
                data_keys=sorted(all_data_keys),
                filter_names=sorted(all_filter_names),
                facet_keys=sorted(all_facet_keys),
                title_cradlepoint_ids=title_cp,
                title_enterprise_wireless_ids=title_ew,
                title_netcloud_ids=title_nc,
                has_company_field=False,
            )
            self.result.metadata["scope_observations"].append({
                "url": self._search_url("", 0),
                "parent_total": parent_total,
                "preview_total": len(seen_ids),
                "first_page_ids": self.result.metadata["enumeration_pages"][0]["preview_job_ids"],
                "last_page_ids": self.result.metadata["enumeration_pages"][-1]["preview_job_ids"],
            })

            # Candidate generators via the board's own query API.
            for query in ("cradlepoint", "Enterprise Wireless", "BEWS"):
                if not self.page_guard():
                    return self.result
                qstart = 0
                qtotal = None
                qids = []
                qok = True
                while True:
                    qurl = self._search_url(query, qstart)
                    qresp = await self.http.get(qurl)
                    self.result.metadata["scope_probe_requests"] += 1
                    if str(qresp.url) != qurl:
                        qok = False
                        break
                    try:
                        qpayload = qresp.json()
                    except ValueError:
                        qok = False
                        break
                    try:
                        qpage = self._preview_page(qpayload, qresp.text, query, qstart)
                    except ValueError:
                        qok = False
                        break
                    if qtotal is None:
                        qtotal = qpage["parent_total"]
                        if not isinstance(qtotal, int) or qtotal < 0:
                            qok = False
                            break
                    elif qpage["parent_total"] != qtotal:
                        qok = False
                        break
                    qids.extend(qpage["preview_job_ids"])
                    if qpage["preview_count"] == 0:
                        break
                    if len(qids) >= qtotal:
                        break
                    qstart += qpage["preview_count"]
                    if not self.page_guard():
                        return self.result
                if not qok or len(qids) != len(set(qids)) or (qtotal is not None and len(qids) != qtotal):
                    return self._changed()
                if not set(qids).issubset(seen_ids):
                    return self._changed()
                self.result.metadata["query_observations"].append(
                    {"query": query, "count": qtotal, "fetched": len(qids), "ids": qids}
                )
            queries = {o["query"]: o for o in self.result.metadata["query_observations"]}
            if queries["cradlepoint"]["count"] != 0 or queries["cradlepoint"]["fetched"] != 0:
                return self._changed()
            candidates = []
            for pid in queries["Enterprise Wireless"]["ids"]:
                if pid not in candidates:
                    candidates.append(pid)
            for pid in queries["BEWS"]["ids"]:
                if pid not in candidates:
                    candidates.append(pid)
            self.result.metadata["candidate_ids"] = candidates
            if not set(candidates).issubset(seen_ids):
                return self._changed()

            # Full detail bodies for every candidate; classify each header.
            cand_started = time.perf_counter()
            for position_id in candidates:
                if not self.page_guard():
                    return self.result
                detail_response = await self.http.get(
                    self.DETAIL_API.format(position_id=position_id)
                )
                self.result.metadata["scope_probe_requests"] += 1
                detail_url = self.DETAIL_API.format(position_id=position_id)
                if str(detail_response.url) != detail_url:
                    return self._changed()
                try:
                    detail_payload = detail_response.json()
                except ValueError:
                    return self._changed()
                try:
                    reference = self._detail_probe(
                        detail_payload, detail_response.text, position_id
                    )
                except ValueError:
                    return self._changed()
                self.result.metadata["candidate_detail_references"].append(reference)
                self.result.metadata["position_detail_references"].append(reference)
                if (
                    reference["position_id"] != str(position_id)
                    or not reference["title"]
                    or reference["description_chars"] < 100
                    or reference["cradlepoint_mentions"] != 0
                    or reference["cradlepoint_mentions_in_description"] != 0
                    or set(reference["data_keys"]) != self.DETAIL_KEYS
                ):
                    return self._changed()
            self.result.timings["candidate_details_seconds"] = _now_seconds(
                time.perf_counter() - cand_started
            )
            refs = self.result.metadata["candidate_detail_references"]
            company_field_keys = sorted(
                {k for ref in refs for k in ref["data_keys"]
                 if any(s in k.lower() for s in self.COMPANY_FIELD_SUBSTRINGS)}
            )
            # Preview keys were already inventoried; detail keys add no company field.
            if company_field_keys:
                return self._changed()
            literal_title = len(title_cp)
            literal_header = sum(1 for r in refs if r.get("cradlepoint_in_header"))
            eews_hiring = sum(
                1 for r in refs if r.get("category") == "eews_entity_hiring_business_header_observed"
            )
            portfolio = sum(1 for r in refs if r.get("category") == "excluded_portfolio_or_product_mention_in_header")
            later = sum(1 for r in refs if r.get("category") == "excluded_benefits_or_later_mention_only")
            token_only = sum(1 for r in refs if r.get("category") == "excluded_token_only")
            counts = {
                "structured_legal_employer_business_unit_brand_team_organization": 0,
                "literal_cradlepoint_in_title": literal_title,
                "literal_cradlepoint_in_header": literal_header,
                "eews_entity_hiring_business_header_observed_excluded_not_literal": eews_hiring,
                "excluded_portfolio_or_product_mention_in_header": portfolio,
                "excluded_benefits_or_later_mention_only": later,
                "excluded_token_only": token_only,
            }
            self.result.metadata.update(
                counts_by_rule=counts,
                structured_company_field_keys=[],
                detail_company_field_keys=company_field_keys,
                borderline_excluded_ids=[r["position_id"] for r in refs if r.get("category") != "literal_cradlepoint_header_hit"],
                eews_entity_hiring_ids=[r["position_id"] for r in refs if r.get("category") == "eews_entity_hiring_business_header_observed"],
            )
            if literal_title != 0 or literal_header != 0:
                return self._changed()
            if counts["structured_legal_employer_business_unit_brand_team_organization"] != 0:
                return self._changed()

            self.result.timings["official_scope_probe_seconds"] = _now_seconds(
                time.perf_counter() - started
            )
            self.result.errors.append(self.SCOPE_ERROR)
            return self.result
        finally:
            if "official_scope_probe_seconds" not in self.result.timings:
                self.result.timings["official_scope_probe_seconds"] = _now_seconds(
                    time.perf_counter() - started
                )
