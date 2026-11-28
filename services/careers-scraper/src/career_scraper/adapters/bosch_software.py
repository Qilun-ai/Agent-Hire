"""Bosch Global Software Technologies' own careers journey and exact global division list."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import html
import json
import math
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import Source


ENTRY = "https://www.bosch-softwaretechnologies.com/en/careers/"
FIELDS_OF_WORK = "https://www.bosch-softwaretechnologies.com/en/careers/fields-of-work/"
COMPANY_SEARCH = "https://careers.smartrecruiters.com/BoschGroup/india"
WORLDWIDE_CAREERS = "https://www.bosch.com/career"
PORTAL_ROOT = "https://jobs.bosch.com/en/"
PAGE_SIZE = 8  # The published global portal's current JobSearch default.
DIVISION_LABEL = "Bosch Global Software Technologies"
DIVISION_ID = "RBEI"  # Must be re-confirmed from the portal's own division options.
API_COLLECTION_SUFFIX = ".content"

ROLE_SECTION_ORDER = ("jobDescription", "qualifications", "additionalInformation")
RENDERED_SECTION_TITLES = {
    "jobDescription": "Your tasks",
    "qualifications": "Your profile",
    "additionalInformation": "Contact & additional information",
}
CORPORATE_SECTION = "companyDescription"
_RENDERED_BLOCK_TAGS = frozenset({
    "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
})


def _rendered_text_from_html(fragment: str | None) -> str:
    """Extract compact browser-style text while keeping the source HTML intact.

    Bosch renders role sections with block elements and inline emphasis. A
    separator between every HTML text node inserts false spaces inside inline
    phrases (for example, across ``<strong>``). Mirror ``innerText``'s block
    breaks and ``br`` elements, then collapse whitespace as the independent
    browser audit does.
    """
    soup = BeautifulSoup(fragment or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    pieces: list[str] = []

    def add_break() -> None:
        if pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            pieces.append(re.sub(r"\s+", " ", str(node)))
            return
        if not isinstance(node, Tag):
            return
        if node.name in {"script", "style", "noscript"}:
            return
        if node.name == "br":
            add_break()
            return
        block = node.name in _RENDERED_BLOCK_TAGS
        if block:
            add_break()
        for child in node.children:
            walk(child)
        if block:
            add_break()

    walk(soup)
    rendered = "".join(pieces)
    rendered = re.sub(r"[\t\f\v\r ]+", " ", rendered)
    rendered = re.sub(r" *\n *", "\n", rendered)
    rendered = re.sub(r"\n+", "\n", rendered)
    return rendered.strip()


def _json_property(html_text: str, prop_name: str):
    soup = BeautifulSoup(html_text, "lxml")
    matches = soup.find_all("script", attrs={"data-prop-name": prop_name})
    if len(matches) != 1:
        raise ValueError(f"Bosch portal must expose one {prop_name} property")
    try:
        return json.loads(matches[0].string or matches[0].get_text())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Bosch portal {prop_name} property is not valid JSON") from exc


def _api_config(html_text: str) -> dict[str, str]:
    match = re.search(r"jobsApi\s*:\s*\{([^}]+)\}", html_text, re.S)
    prefix = re.search(r"jobAdLinkPrefix\s*:\s*['\"]([^'\"]+)['\"]", html_text)
    if not match or not prefix:
        raise ValueError("Bosch global portal API or exact detail URL prefix is missing")
    config = {
        name: value
        for name, value in re.findall(r"\b(baseUrl|tenant|project|collection|apiKey)\s*:\s*['\"]([^'\"]+)['\"]", match.group(1))
    }
    if set(config) != {"baseUrl", "tenant", "project", "collection", "apiKey"}:
        raise ValueError("Bosch public job API configuration changed")
    config["jobAdLinkPrefix"] = prefix.group(1)
    return config


def _visible_link(soup: BeautifulSoup, predicate, *, description: str):
    matches = []
    for anchor in soup.select("a[href]"):
        label = " ".join((anchor.get_text(" ", strip=True) or anchor.get("aria-label", "")).split())
        href = urljoin(ENTRY, anchor["href"])
        if predicate(label, href):
            matches.append((label, href))
    if len(matches) != 1:
        raise ValueError(f"Bosch official page must expose exactly one {description}")
    return matches[0]


def _country_filter_ids(options) -> tuple[list[str], list[dict]]:
    if not isinstance(options, list) or not options:
        raise ValueError("Bosch global country filter options are missing")
    ids = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("ids"), list):
            raise ValueError("Bosch global country option has an unknown schema")
        for identity in option["ids"]:
            if isinstance(identity, str) and identity and identity not in ids:
                # Preserve publisher values exactly, including legacy trailing whitespace.
                ids.append(identity)
    india = [option for option in options if str(option.get("label", "")).strip() == "India"]
    if len(india) != 1 or "in" not in india[0]["ids"]:
        raise ValueError("Bosch global country filters no longer expose India")
    return ids, options


def _division_filter(options) -> dict:
    if not isinstance(options, list) or not options:
        raise ValueError("Bosch global division filter options are missing")
    matches = [
        option for option in options
        if isinstance(option, dict)
        and str(option.get("label", "")).strip() == DIVISION_LABEL
    ]
    if len(matches) != 1 or matches[0].get("ids") != [DIVISION_ID]:
        raise ValueError("Bosch global BGSW division option changed; membership needs review")
    return matches[0]


def _source_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _custom_fields(job: dict) -> list[dict]:
    fields = job.get("customField")
    return fields if isinstance(fields, list) else []


def _field_value(job: dict, label: str):
    matches = [field for field in _custom_fields(job) if field.get("fieldLabel") == label]
    if len(matches) > 1:
        raise ValueError(f"Bosch detail has repeated {label!r} custom fields")
    return matches[0].get("valueLabel") if matches else None


def _description_sections(detail: dict) -> tuple[list[dict], str]:
    raw_sections = detail.get("jobAd", {}).get("sections")
    if not isinstance(raw_sections, dict):
        return [], ""
    keys = [key for key in ROLE_SECTION_ORDER if key in raw_sections]
    keys.extend(key for key in raw_sections if key not in ROLE_SECTION_ORDER and key != CORPORATE_SECTION)
    sections = []
    fragments = []
    for key in keys:
        section = raw_sections.get(key)
        if not isinstance(section, dict) or not isinstance(section.get("text"), str):
            continue
        source_title = section.get("title") or key
        title = RENDERED_SECTION_TITLES.get(key, source_title)
        body = section["text"]
        sections.append({"key": key, "title": source_title, "rendered_title": title, "html": body})
        if body.strip():
            fragments.append(f"<section data-bosch-section=\"{html.escape(key, quote=True)}\">"
                             f"<h2>{html.escape(str(title))}</h2>{body}</section>")
    return sections, "\n".join(fragments)


def _apply_url(detail: dict) -> str | None:
    value = detail.get("applyUrl")
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != "jobs.smartrecruiters.com":
        raise ValueError("Bosch exact-job apply URL left the published SmartRecruiters host")
    query = parse_qsl(parts.query, keep_blank_values=True)
    trid = detail.get("trid")
    if trid and not any(key == "trid" for key, _ in query):
        # The rendered Bosch detail page appends its exact exposed trid value.
        query.append(("trid", str(trid)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _canonical_json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(payload).hexdigest()


class BoschSoftware(Adapter):
    """Use only the BGSW-published global portal and its exact RBEI division option."""

    VERIFICATION_REVISION = "bosch-software.company-published-global-rbei.1"

    def __init__(self, http, company, source: Source, options):
        super().__init__(http, company, source, options)
        self.api_key = None
        self.api_collection = None
        self.job_url_prefix = None
        self.country_ids = []
        self.country_options = []
        self.division_option = None
        self.expected_total = None
        self.source_started = time.perf_counter()
        self.requested_refs: set[str] = set()

    async def _discover_published_scope(self):
        entry = await self.http.get(ENTRY)
        entry_soup = BeautifulSoup(entry.text, "lxml")
        official_search_label, company_search = _visible_link(
            entry_soup,
            lambda label, href: ("job search" in label.lower() or "find jobs" in label.lower())
            and urlsplit(href).hostname == "careers.smartrecruiters.com"
            and urlsplit(href).path.rstrip("/") == "/BoschGroup/india",
            description="company-published India job-search link",
        )
        if "Bosch Global Software Technologies Private Limited" not in entry_soup.get_text(" ", strip=True):
            raise ValueError("Bosch official careers entry no longer identifies BGSW Private Limited")

        fields_response = await self.http.get(FIELDS_OF_WORK)
        fields_soup = BeautifulSoup(fields_response.text, "lxml")
        worldwide_label, worldwide_url = _visible_link(
            fields_soup,
            lambda label, href: label.lower() == "worldwide job opportunities"
            and urlsplit(href).hostname in {"www.bosch.com", "bosch.com"}
            and urlsplit(href).path.rstrip("/") == "/career",
            description="company-published worldwide job route",
        )

        company_search_response = await self.http.get(company_search)
        company_search_final = str(company_search_response.url)
        if (urlsplit(company_search_final).hostname != "jobs.bosch.com"
                or urlsplit(company_search_final).path.rstrip("/") != "/en"):
            raise ValueError("BGSW-published India job route no longer reaches the observed Bosch career portal")

        global_response = await self.http.get(worldwide_url)
        global_soup = BeautifulSoup(global_response.text, "lxml")
        if (urlsplit(str(global_response.url)).hostname not in {"www.bosch.com", "bosch.com"}
                or urlsplit(str(global_response.url)).path.rstrip("/") != "/careers"):
            raise ValueError("BGSW worldwide route changed its Bosch Global career landing page")
        portal_label, portal_url = _visible_link(
            global_soup,
            lambda label, href: label.lower().startswith("find jobs worldwide")
            and urlsplit(href).hostname == "jobs.bosch.com"
            and urlsplit(href).path.rstrip("/") == "/en",
            description="Bosch Global published job-search link",
        )
        portal_response = await self.http.get(portal_url)
        if (urlsplit(str(portal_response.url)).hostname != "jobs.bosch.com"
                or urlsplit(str(portal_response.url)).path.rstrip("/") != "/en"):
            raise ValueError("Bosch Global job-search link changed its public portal")

        config = _api_config(portal_response.text)
        self.api_key = config.pop("apiKey")
        self.job_url_prefix = config.pop("jobAdLinkPrefix")
        if urlsplit(self.job_url_prefix).hostname != "jobs.bosch.com":
            raise ValueError("Bosch job detail prefix is outside the inspected public portal")
        self.api_collection = (
            f"{config['baseUrl'].rstrip('/')}/{config['tenant']}/"
            f"{config['project']}.{config['collection']}{API_COLLECTION_SUFFIX}"
        )
        self.country_ids, self.country_options = _country_filter_ids(
            _json_property(portal_response.text, "countryFilterOptions")
        )
        self.division_option = _division_filter(
            _json_property(portal_response.text, "divisionFilterOptions")
        )
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=str(entry.url),
            company_search_label=official_search_label,
            company_search_url=company_search,
            company_search_current_final_url=company_search_final,
            worldwide_link_label=worldwide_label,
            worldwide_link_url=worldwide_url,
            global_careers_final_url=str(global_response.url),
            global_portal_label=portal_label,
            global_portal_url=portal_url,
            global_portal_final_url=str(portal_response.url),
            listing_api_base=self.api_collection,
            detail_url_prefix=self.job_url_prefix,
            listing_request_shape={
                "method": "GET",
                "endpoint": f"{self.api_collection}/_aggrs/get_jobs",
                "avars": {
                    "country": "all exact IDs in the portal's current countryFilterOptions",
                    "division": self.division_option["ids"],
                    "sort": {"releasedDate": -1},
                    "page_language": "en",
                },
                "pagesize": PAGE_SIZE,
                "page": "1-based source pagination",
                "auth": "public Authorization Bearer header exposed to the official portal; secret omitted",
            },
            published_scope={
                "basis": "BGSW careers page publishes the worldwide Bosch careers route; the same global portal exposes exact Bosch Global Software Technologies division filter.",
                "division_label": self.division_option["label"],
                "division_ids": self.division_option["ids"],
                "country_filter_options": len(self.country_options),
                "country_filter_ids": len(self.country_ids),
                "country_filter_values": [
                    {"label": option.get("label"), "ids": option.get("ids")}
                    for option in self.country_options
                ],
                "all_country_filter_ids": self.country_ids,
                "country_filters_applied": "all portal-published global country filter IDs",
                "other_division_labels_excluded": [
                    str(option.get("label")) for option in _json_property(portal_response.text, "divisionFilterOptions")
                    if isinstance(option, dict)
                    and "Bosch Global Software Technologies" in str(option.get("label", ""))
                    and option.get("ids") != self.division_option["ids"]
                ],
                "legal_employer": "Retain each posting's own legal_entity value; the India division's legacy source ID is RBEI.",
            },
            source_probe_seconds=round(time.perf_counter() - self.source_started, 6),
        )
        self.result.timings["official_scope_source_seconds"] = round(time.perf_counter() - self.source_started, 6)

    def _listing_url(self):
        return f"{self.api_collection}/_aggrs/get_jobs"

    def _listing_avars(self):
        return {
            "country": self.country_ids,
            "division": self.division_option["ids"],
            "sort": {"releasedDate": -1},
            "page_language": "en",
        }

    async def _listing_page(self, page: int):
        if not self.page_guard():
            raise ValueError("Bosch global BGSW listing pagination budget exhausted")
        params = {
            "avars": json.dumps(self._listing_avars(), ensure_ascii=False, separators=(",", ":")),
            "pagesize": PAGE_SIZE,
            "page": page,
        }
        response = await self.http.json(
            self._listing_url(), params=params,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        embedded = response.get("_embedded", {}) if isinstance(response, dict) else {}
        values = embedded.get("rh:result") if isinstance(embedded, dict) else None
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError(f"Bosch BGSW listing page {page} returned an unknown aggregate response")
        result = values[0]
        meta = result.get("meta")
        rows = result.get("data")
        if (not isinstance(meta, list) or not meta or not isinstance(meta[0].get("count"), int)
                or meta[0]["count"] < 0 or not isinstance(rows, list) or len(rows) > PAGE_SIZE):
            raise ValueError(f"Bosch BGSW listing page {page} has invalid count/data fields")
        total = meta[0]["count"]
        if total == 0:
            raise ValueError("Bosch BGSW global filter returned zero; this is not a certified empty result")
        return rows, total

    @staticmethod
    def _validate_listing_row(row: dict, *, page: int):
        identity = row.get("refNumber") or row.get("_id")
        if (not identity or row.get("_id") != identity or not isinstance(row.get("name"), str)
                or not row.get("name", "").strip() or not isinstance(row.get("jobUrl"), str)
                or not row.get("jobUrl", "").strip()):
            raise ValueError(f"Bosch BGSW listing page {page} contains a missing identity/title/job URL")
        division = row.get("division")
        if not isinstance(division, dict) or division.get("valueId") != DIVISION_ID:
            raise ValueError(f"Bosch listing {identity} is outside exact BGSW division RBEI")
        if not isinstance(row.get("location"), dict) or not row["location"].get("country"):
            raise ValueError(f"Bosch listing {identity} has no published country/location object")
        return str(identity)

    async def _snapshot(self, *, name: str):
        started = time.perf_counter()
        all_rows = []
        page_hashes = []
        page_counts = []
        ids = set()
        page = 1
        total_expected = None
        while True:
            rows, total = await self._listing_page(page)
            if total_expected is None:
                total_expected = total
                if self.expected_total is None:
                    self.expected_total = total
                    self.result.expected_total = total
                elif total != self.expected_total:
                    self.result.errors.append(f"bosch_{name}_total_changed:{total}!={self.expected_total}")
            elif total != total_expected or total != self.expected_total:
                self.result.errors.append(f"bosch_{name}_page_total_changed:{page}:{total}")
            if page < math.ceil(total / PAGE_SIZE) and len(rows) != PAGE_SIZE:
                self.result.errors.append(f"bosch_{name}_short_page:{page}:{len(rows)}")
            if page == math.ceil(total / PAGE_SIZE) and len(rows) != total - PAGE_SIZE * (page - 1):
                self.result.errors.append(f"bosch_{name}_last_page_size_mismatch:{page}:{len(rows)}")
            if not rows:
                self.result.errors.append(f"bosch_{name}_pagination_ended_early:{page}")
                break
            for row in rows:
                identity = self._validate_listing_row(row, page=page)
                if identity in ids:
                    self.result.errors.append(f"bosch_{name}_duplicate_listing_id:{identity}")
                ids.add(identity)
                all_rows.append(row)
            page_hashes.append(_canonical_json_hash(rows))
            page_counts.append(len(rows))
            if page >= math.ceil(total / PAGE_SIZE):
                break
            page += 1
        snapshot = {
            "name": name,
            "expected_total": total_expected,
            "page_size": PAGE_SIZE,
            "pages": len(page_counts),
            "page_counts": page_counts,
            "page_sha256": page_hashes,
            "ordered_ids": [str(row["refNumber"]) for row in all_rows],
            "row_sha256": {str(row["refNumber"]): _canonical_json_hash(row) for row in all_rows},
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        self.result.timings[f"{name}_listing_seconds"] = snapshot["duration_seconds"]
        return all_rows, snapshot

    async def _detail(self, job):
        identity = str(job.job_id)
        if identity not in self.requested_refs:
            raise ValueError(f"Bosch exact detail {identity} was not in the inspected company list")
        filter_value = json.dumps({"refNumber": identity}, separators=(",", ":"))
        detail_url = f"{self.api_collection}/?np&rep=pj&filter={quote(filter_value, safe='')}"
        documents = await self.http.json(
            detail_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
            raise ValueError(f"Bosch exact detail {identity} returned an unknown document list")
        return self._apply_detail(job, documents[0], detail_url)

    def _apply_detail(self, job, detail: dict, detail_url: str):
        identity = str(job.job_id)
        division_fields = [
            field for field in _custom_fields(detail)
            if field.get("valueId") == DIVISION_ID and field.get("valueLabel") == "BGSW"
        ]
        if (detail.get("refNumber") != identity or detail.get("name") != job.title
                or detail.get("jobUrl") != job.raw["listing"].get("jobUrl")
                or detail.get("visibility") != "PUBLIC" or not division_fields
                or detail.get("company", {}).get("identifier") != "BoschGroup"
                ):
            raise ValueError(f"Bosch exact detail {identity} does not match its current public BGSW listing")
        detail_sections, description = _description_sections(detail)
        job.raw["detail"] = detail
        job.raw["detail_api_url"] = detail_url
        job.raw["role_description_sections"] = detail_sections
        job.raw["company_description_section"] = detail.get("jobAd", {}).get("sections", {}).get(CORPORATE_SECTION)
        job.raw["description_links"] = [
            {"url": anchor.get("href"), "text": anchor.get_text(" ", strip=True), "section": section["key"]}
            for section in detail_sections
            for anchor in BeautifulSoup(section["html"], "lxml").select("a[href]")
        ]
        job.warnings = [warning for warning in job.warnings if warning != "detail_fetch_failed"]
        if detail.get("active") is False:
            job.warnings.append("public_listing_detail_active_flag_false")
        job.description_html = description
        job.description_text = _rendered_text_from_html(description)
        job.description_method = "bosch.jobs_api.exact_public_detail.role_sections"
        job.description_source_url = job.url
        job.requisition_id = str(detail.get("id")) if detail.get("id") is not None else None
        job.apply_url = _apply_url(detail)
        job.locations = [detail["location"]] if isinstance(detail.get("location"), dict) else []
        job.department = detail.get("function")
        job.team = job.raw["listing"].get("division") or {
            "valueId": next((field.get("valueId") for field in _custom_fields(detail)
                             if field.get("fieldLabel") == "Division"), None),
            "valueLabel": _field_value(detail, "Division full name"),
        }
        job.employment_type = detail.get("typeOfEmployment", {}).get("label")
        job.workplace_type = job.raw["listing"].get("work_mode")
        job.experience = detail.get("experienceLevel", {}).get("label")
        job.education = None  # The source publishes qualifications in full HTML, not a structured education field.
        salary_fields = [
            field for field in _custom_fields(detail)
            if re.search(r"salar|compensation|pay", str(field.get("fieldLabel", "")), re.I)
        ]
        job.salary = salary_fields or None
        job.qualifications = next((s["html"] for s in detail_sections if s["key"] == "qualifications"), None)
        job.responsibilities = next((s["html"] for s in detail_sections if s["key"] == "jobDescription"), None)
        listing_legal_entity = job.raw["listing"].get("legal_entity")
        job.employer = (
            listing_legal_entity.get("valueLabel") if isinstance(listing_legal_entity, dict)
            else None
        ) or _field_value(detail, "Legal Entity")
        job.language = detail.get("language", {}).get("code")
        raw_posted = detail.get("releasedDate") or job.raw["listing"].get("releasedDate")
        job.date_posted = _source_date(raw_posted)
        job.date_posted_raw = raw_posted
        # This publisher's creator/template timestamps are not posting or deadline dates.
        job.date_updated = detail.get("dateUpdated") or None
        job.valid_through = detail.get("validThrough") or None
        if not description.strip():
            job.description_unavailable = True
            job.warnings.append("description_not_full_field")
        return job

    async def run(self):
        await self._discover_published_scope()
        initial_rows, initial = await self._snapshot(name="initial")
        initial_by_id = {str(row["refNumber"]): row for row in initial_rows}
        if len(initial_rows) != self.expected_total:
            self.result.errors.append("bosch_initial_listing_membership_count_mismatch")
        self.requested_refs = set(initial_by_id)
        for row in initial_rows:
            identity = str(row["refNumber"])
            url = urljoin(self.job_url_prefix, row["jobUrl"])
            if (urlsplit(url).hostname != "jobs.bosch.com"
                    or urlsplit(url).path.rsplit("/", 1)[-1] != row["jobUrl"]):
                raise ValueError(f"Bosch listing {identity} exposed a noncanonical detail URL")
            legal_entity = row.get("legal_entity")
            self.add(self.job(
                {"listing": row}, title=row["name"], job_id=identity, url=url,
                date_posted=_source_date(row.get("releasedDate")),
                date_posted_raw=row.get("releasedDate"),
                employer=legal_entity.get("valueLabel") if isinstance(legal_entity, dict) else None,
                locations=[row["location"]],
                department=row.get("function"),
            ))
            if self.limit():
                break
        self.requested_refs = {str(job.job_id) for job in self.result.jobs}
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        if "sample_limit" not in self.result.warnings:
            final_rows, final = await self._snapshot(name="final")
            final_by_id = {str(row["refNumber"]): row for row in final_rows}
            self.result.metadata["initial_listing_ids"] = initial["ordered_ids"]
            self.result.metadata["final_listing_ids"] = final["ordered_ids"]
            if initial["ordered_ids"] != final["ordered_ids"] or initial_by_id != final_by_id:
                self.result.errors.append("bosch_initial_final_full_listing_fields_or_membership_changed")
            if len(final_rows) != self.expected_total or set(final_by_id) != set(initial_by_id):
                self.result.errors.append("bosch_final_listing_ids_do_not_reconcile")
        else:
            self.result.metadata["initial_listing_ids"] = initial["ordered_ids"]
            self.result.metadata["final_listing_ids"] = None
        self.result.metadata["company_published_job_ids"] = initial["ordered_ids"]
        self.result.metadata["sample_detail_refs"] = {
            "first": initial["ordered_ids"][0] if initial["ordered_ids"] else None,
            "last": initial["ordered_ids"][-1] if initial["ordered_ids"] else None,
            "first_url": self.result.jobs[0].url if self.result.jobs else None,
            "last_url": next((job.url for job in self.result.jobs if job.job_id == (initial["ordered_ids"][-1] if initial["ordered_ids"] else None)), None),
            "independent_rendered_references": "pending parent-supplied CUA evidence",
        }
        self.result.listing_complete = (
            "sample_limit" not in self.result.warnings
            and not any(error.startswith("bosch_") for error in self.result.errors)
            and len(initial_rows) == self.expected_total
        )
        return self.result
