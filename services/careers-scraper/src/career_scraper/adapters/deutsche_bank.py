"""Deutsche Bank professionals search: job-module SPA flow on Beesite endpoints.

Observed journey (see companies/deutsche-bank/research/): the official entry
page ships ``<div id="job-module" data-jobmodule="PROFESSIONAL">`` plus
``careersJs.js``.  That script attaches ``jobplatform.js`` when ``#job-module``
exists.  The Vue app then issues unfiltered searches against the Beesite
professional-search endpoint it embeds (``api-deutschebank.beesite.de/search``)
with an expanding ``CountItem`` window (10, 20, 30, ...) until
``SearchResultCount`` reaches ``SearchResultCountAll``, and opens a detail by
fetching ``jobhtml/<PositionID>.json`` for the exact listed ID.  The adapter
replays only those requests, in that shape, and never enumerates the
graduate/azubi boards or any other ATS surface.
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

import html as _html

API_HOST = "api-deutschebank.beesite.de"
JOB_ID = re.compile(r"^\d+$")
# The header value is either a Workday requisition (R0000000) or a legacy
# recruitment-platform requisition (DBFB#####); both use the same labeled row.
REQUISITION = re.compile(r"Job ID:\s*</strong>\s*([A-Za-z0-9][A-Za-z0-9_-]*)")
APPLY_REQUISITION = re.compile(r"_R(\d{7})/apply")
APPLY_JOB_ID = re.compile(r"jobId=([A-Za-z0-9-]+)")
FULL_PART_TIME = re.compile(r"(?:Full/Part-Time|Voll-/Teilzeit):\s*</strong>\s*([^<]+)", re.I)
REGULAR_TEMPORARY = re.compile(r"Regular/Temporary:\s*</strong>\s*([^<]+)", re.I)
LISTED_DATE = re.compile(r"Listed:\s*</strong>\s*([^<]+)", re.I)
EMPLOYER = re.compile(r"Employer:\s*</b>\s*<b>([^<]+)</b>", re.I)
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)


def native_job_url(entry_url, identity):
    if not JOB_ID.fullmatch(str(identity)):
        raise ValueError(f"Deutsche Bank listed PositionID is not numeric: {identity!r}")
    return f"{entry_url.rstrip('/')}#/professional/job/{identity}"


def jobhtml_url(jobhtml_base, identity):
    if not JOB_ID.fullmatch(str(identity)):
        raise ValueError(f"Deutsche Bank PositionID is not numeric: {identity!r}")
    return f"{jobhtml_base}{identity}.json"


def detail_h1_title(html_text):
    soup = BeautifulSoup(html_text or "", "lxml")
    heading = soup.find("h1")
    return heading.get_text(" ", strip=True) if heading else ""


def _same_title(listed, detail):
    """Listing titles arrive with HTML entities (e.g. &amp;) intact; the
    detail h1 is decoded by the parser.  Compare decoded, collapsed forms."""
    return re.sub(r"\s+", " ", _html.unescape(str(listed or ""))).strip() == \
        re.sub(r"\s+", " ", _html.unescape(str(detail or ""))).strip()


def _all_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


BOILERPLATE_MARKERS = ("compliance/posters", "equal-opportunity", "benefit-highlights")
GOV_POSTER_HOSTS = ("eeoc.gov", "dol.gov")


def is_compliance_boilerplate(href):
    """EEO/labor-law posters, policy statements and benefit brochures repeat
    verbatim across US postings; they are not role-bearing attachments."""
    host = (urlsplit(href or "").hostname or "").lower()
    if any(host.endswith(marker) for marker in GOV_POSTER_HOSTS):
        return True
    lowered = (href or "").lower()
    return any(marker in lowered for marker in BOILERPLATE_MARKERS)


def attachment_links(description_html):
    """Role-bearing attachment links only (anything needing full-text capture)."""
    return [link for link in _all_attachment_links(description_html)
            if not is_compliance_boilerplate(link["url"])]


def compliance_links(description_html):
    """Boilerplate compliance/brochure links, retained as data."""
    return [link for link in _all_attachment_links(description_html)
            if is_compliance_boilerplate(link["url"])]


def search_url(search_base, descriptors, sort_criteria, language_code, count_item):
    payload = {
        "LanguageCode": language_code,
        "SearchParameters": {
            "FirstItem": 1,
            "CountItem": count_item,
            "MatchedObjectDescriptor": descriptors,
            "Sort": sort_criteria,
        },
        "SearchCriteria": [],
    }
    # JSON.stringify order/shape observed in the company bundle.
    return search_base + "/?data=" + json.dumps(payload, separators=(",", ":"))


def listed_locations(descriptor):
    locations = []
    for location in descriptor.get("PositionLocation") or []:
        if not isinstance(location, dict):
            continue
        entry = {"location": location.get("CityName")}
        if location.get("CountryName") is not None:
            entry["country"] = location["CountryName"]
        if location.get("CountrySubDivisionName") is not None:
            entry["subdivision"] = location["CountrySubDivisionName"]
        locations.append(entry)
    return locations


class DeutscheBank(Adapter):
    VERIFICATION_REVISION = "deutschebank.jobmodule-professional-search-and-jobhtml.1"

    @staticmethod
    def _assert_careers_url(url, *, path_prefix="/professionals/search-roles"):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"careers.db.com", "www.careers.db.com"}:
            raise ValueError(f"Deutsche Bank company page left the official HTTPS site: {url}")
        if (parsed.path.rstrip("/") not in {path_prefix, path_prefix + "/index"}
                and not parsed.path.startswith(path_prefix + "/")):
            raise ValueError(f"Deutsche Bank company page has an unexpected path: {url}")
        return parsed

    async def _company_script(self, page_url, script_src, description):
        script_url = urljoin(str(page_url), script_src)
        self._assert_careers_url(script_url, path_prefix="/application/project/js")
        response = await self.http.get(script_url)
        final = urlsplit(str(response.url))
        if (final.hostname not in {"careers.db.com", "www.careers.db.com"}
                or not final.path.startswith("/application/project/js")):
            raise ValueError(f"Deutsche Bank {description} script redirected away from the company site: {response.url}")
        return str(response.url), response.text

    @staticmethod
    def _jobplatform_source(script):
        """Extract the embedded Beesite endpoints and request constants."""
        search_bases = re.findall(r'PROFESSIONAL:"(https://api-deutschebank[^"]+/search)"', script)
        if len(search_bases) != 1:
            raise ValueError("Deutsche Bank jobplatform no longer embeds exactly one professional search endpoint")
        search_base = search_bases[0]
        parsed = urlsplit(search_base)
        if (parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.path != "/search"
                or parsed.query or parsed.fragment):
            raise ValueError(f"Deutsche Bank embedded professional search endpoint changed: {search_base}")
        jobhtml = re.findall(
            r'JOB_OVERVIEW:function\(t\)\{return"(https://api-deutschebank[^"]+/jobhtml/)"\.concat\(t,"\.json"\)\}',
            script,
        )
        if len(jobhtml) != 1:
            raise ValueError("Deutsche Bank jobplatform no longer embeds the observed jobhtml detail template")
        jobhtml_base = jobhtml[0]
        parsed = urlsplit(jobhtml_base)
        if parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.path != "/jobhtml/":
            raise ValueError(f"Deutsche Bank embedded detail endpoint changed: {jobhtml_base}")
        if 'LOOKUPS:"https://api-deutschebank.beesite.de/lookup/"' not in script:
            raise ValueError("Deutsche Bank jobplatform lookup endpoint changed from its observed source")
        if 'di.a.get("".concat(t,"/?data=").concat(JSON.stringify(i)))' not in script:
            raise ValueError("Deutsche Bank jobplatform search request shape changed from ?data=<JSON>")
        if "LanguageCode:" not in script or "document.documentElement.lang" not in script:
            raise ValueError("Deutsche Bank jobplatform search language source changed from its observed request")
        descriptors = None
        for match in re.finditer(r"\bAi=(\[.*?\])", script):
            try:
                candidate = json.loads(match.group(1))
            except ValueError:
                continue
            if isinstance(candidate, list) and "PositionID" in candidate and "PositionTitle" in candidate:
                descriptors = candidate
                break
        if not descriptors:
            raise ValueError("Deutsche Bank jobplatform no longer exposes its MatchedObjectDescriptor list")
        sort_criteria = None
        for match in re.finditer(r"\bEi=(\[.*?\])", script):
            block = match.group(1)
            criterion = re.search(r'Criterion:"([^"]+)"', block)
            direction = re.search(r'Direction:"([^"]+)"', block)
            if criterion and direction:
                sort_criteria = [{"Criterion": criterion.group(1), "Direction": direction.group(1)}]
                break
        if sort_criteria != [{"Criterion": "PublicationStartDate", "Direction": "DESC"}]:
            raise ValueError("Deutsche Bank jobplatform search sort changed from PublicationStartDate DESC")
        if 'path:"job/:job_id"' not in script or '{name:"JobPage"' not in script:
            raise ValueError("Deutsche Bank jobplatform detail route changed from professional job/:job_id")
        if "params:{job_id:t.result.id}" not in script:
            raise ValueError("Deutsche Bank jobplatform result link no longer targets the exact listed job_id")
        if "new Ci(t))" not in script or "SearchResultItems" not in script:
            raise ValueError("Deutsche Bank jobplatform result-list parsing changed from its observed flow")
        if "fetchJobOverview" not in script or "r.json()" not in script:
            raise ValueError("Deutsche Bank jobplatform detail fetch changed from its observed jobhtml request")
        return search_base, jobhtml_base, descriptors, sort_criteria

    @staticmethod
    def _listing_rows(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("SearchResult"), dict):
            raise ValueError("Deutsche Bank search response has no SearchResult object")
        result = payload["SearchResult"]
        if not isinstance(result.get("SearchResultItems"), list):
            raise ValueError("Deutsche Bank search response has no SearchResultItems list")
        counts = {}
        for key in ("SearchResultCount", "SearchResultCountAll"):
            value = result.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Deutsche Bank search response has no valid {key}")
            counts[key] = value
        rows = []
        for item in result["SearchResultItems"]:
            descriptor = item.get("MatchedObjectDescriptor") if isinstance(item, dict) else None
            if not isinstance(descriptor, dict):
                raise ValueError("Deutsche Bank search item has no MatchedObjectDescriptor")
            identity = descriptor.get("PositionID")
            if not JOB_ID.fullmatch(str(identity)):
                raise ValueError(f"Deutsche Bank search item has a missing or invalid PositionID: {identity!r}")
            if not str(descriptor.get("PositionTitle") or "").strip():
                raise ValueError(f"Deutsche Bank search item {identity} has no PositionTitle")
            rows.append((str(identity), descriptor, item))
        ids = [identity for identity, _, _ in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("Deutsche Bank search response repeats a company-published PositionID")
        if counts["SearchResultCount"] != len(rows):
            raise ValueError("Deutsche Bank search displayed count differs from returned items")
        return counts, rows

    async def _search_snapshot(self, search_base, descriptors, sort_criteria, language_code, count_item, label):
        url = search_url(search_base, descriptors, sort_criteria, language_code, count_item)
        response = await self.http.get(url)
        if "api-deutschebank.beesite.de/search" not in str(response.url):
            raise ValueError(f"Deutsche Bank {label} search request left its inspected source: {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Deutsche Bank {label} search response is not JSON") from exc
        counts, rows = self._listing_rows(payload)
        return payload, counts, rows

    @staticmethod
    def _parse_detail(payload, identity):
        if not isinstance(payload, dict):
            raise ValueError(f"{identity}: Deutsche Bank jobhtml response is not a JSON object")
        html = payload.get("html")
        apply_url = payload.get("apply_uri")
        if not isinstance(html, str) or not html.strip():
            raise ValueError(f"{identity}: Deutsche Bank jobhtml html is empty")
        if not isinstance(apply_url, str) or not apply_url.startswith("https://"):
            raise ValueError(f"{identity}: Deutsche Bank jobhtml apply_uri is missing or not HTTPS")
        match = REQUISITION.search(html)
        requisition = match.group(1) if match else None
        if not requisition:
            apply_match = APPLY_REQUISITION.search(apply_url) or APPLY_JOB_ID.search(apply_url)
            if apply_match:
                requisition = apply_match.group(1)
                if requisition.isdigit() and len(requisition) == 7:
                    requisition = "R" + requisition
        if not requisition:
            raise ValueError(f"{identity}: Deutsche Bank detail exposes no requisition Job ID")
        facts = {
            "requisition_id": requisition,
            "h1_title": detail_h1_title(html),
            "full_part_time": (FULL_PART_TIME.search(html).group(1).strip()
                               if FULL_PART_TIME.search(html) else None),
            "regular_temporary": (REGULAR_TEMPORARY.search(html).group(1).strip()
                                  if REGULAR_TEMPORARY.search(html) else None),
            "listed_date": (LISTED_DATE.search(html).group(1).strip()
                            if LISTED_DATE.search(html) else None),
            "employer": (EMPLOYER.search(html).group(1).strip()
                         if EMPLOYER.search(html) else None),
            "save_uri": payload.get("save_uri"),
            "pro_div_id": payload.get("pro_div_id"),
            "pro_div_name": payload.get("pro_div_name"),
            "pro_div_code": payload.get("pro_div_code"),
        }
        return html, apply_url, facts

    def _make_job(self, identity, descriptor, item, detail, *, entry_url, search_base, jobhtml_base,
                  language_code):
        html, apply_url, facts = detail
        title = str(descriptor["PositionTitle"]).strip()
        description_text = text_html(html)
        if len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Deutsche Bank full detail description is implausibly short")
        if facts["h1_title"] and not _same_title(title, facts["h1_title"]):
            self.result.errors.append(f"{identity}: Deutsche Bank detail h1 title differs from the listed title")
        locations = listed_locations(descriptor)
        if not locations:
            self.result.errors.append(f"{identity}: Deutsche Bank listed job has no PositionLocation")
        links = attachment_links(html)
        boilerplate = compliance_links(html)
        if links:
            self.result.errors.append(
                f"{identity}: Deutsche Bank detail links role attachment files without full attachment text"
            )
        date_posted = descriptor.get("PublicationStartDate")
        if date_posted is not None and not isinstance(date_posted, str):
            date_posted = None
        if facts["listed_date"] and date_posted and facts["listed_date"] != date_posted:
            self.result.errors.append(f"{identity}: Deutsche Bank listed date differs from the detail Listed date")
        apply_requisition = APPLY_REQUISITION.search(apply_url)
        if apply_requisition and apply_requisition.group(1) not in facts["requisition_id"]:
            self.result.errors.append(f"{identity}: Deutsche Bank apply link requisition differs from the detail Job ID")
        def names(entries):
            return [entry.get("Name") for entry in (entries or []) if isinstance(entry, dict)]
        raw = {
            "listing": item,
            "detail": {
                "apply_uri": apply_url,
                "save_uri": facts["save_uri"],
                "pro_div_id": facts["pro_div_id"],
                "pro_div_name": facts["pro_div_name"],
                "pro_div_code": facts["pro_div_code"],
            },
            "source_ui": {
                "native_job_url": native_job_url(entry_url, identity),
                "listed_position_uri": descriptor.get("PositionURI"),
                "rendered_apply_url": apply_url,
                "search_base_url": search_base,
                "jobhtml_url": jobhtml_url(jobhtml_base, identity),
                "language_code": language_code,
                "organization_name": descriptor.get("OrganizationName"),
                "position_hiring_year": descriptor.get("PositionHiringYear"),
                "career_level_names": names(descriptor.get("CareerLevel")),
                "position_offering_type_names": names(descriptor.get("PositionOfferingType")),
                "position_schedule_names": names(descriptor.get("PositionSchedule")),
                "publication_channel": descriptor.get("PublicationChannel"),
                "detail_header": facts,
                "attachment_links": links,
                "compliance_boilerplate_links": boilerplate,
            },
        }
        job = self.job(
            raw,
            title=title,
            url=native_job_url(entry_url, identity),
            job_id=identity,
            requisition_id=facts["requisition_id"],
            apply_url=apply_url,
            description=html,
            method="deutsche_bank.jobmodule.beesite_search_and_jobhtml",
            description_url=jobhtml_url(jobhtml_base, identity),
            locations=locations,
            department=facts["pro_div_name"] or None,
            employment_type=facts["full_part_time"],
            date_posted=date_posted,
            date_posted_raw=descriptor.get("PublicationStartDate"),
            date_updated=None,
            salary=None,
            employer=facts["employer"],
        )
        job.description_text = description_text
        job.record_identity = f"deutsche_bank:{identity}"
        return job

    def _placeholder(self, identity, descriptor, item, *, entry_url, search_base, jobhtml_base, language_code):
        """Listing-only record used as hydrate input; detail is hydrated next."""
        job = self.job(
            {"listing": item, "detail": None, "source_ui": {
                "native_job_url": native_job_url(entry_url, identity),
                "listed_position_uri": descriptor.get("PositionURI"),
                "search_base_url": search_base,
                "jobhtml_url": jobhtml_url(jobhtml_base, identity),
                "language_code": language_code,
            }},
            title=str(descriptor["PositionTitle"]).strip(),
            url=native_job_url(entry_url, identity),
            job_id=identity,
            description="",
            method=None,
            description_url=jobhtml_url(jobhtml_base, identity),
            locations=listed_locations(descriptor),
            date_posted=descriptor.get("PublicationStartDate"),
            date_posted_raw=descriptor.get("PublicationStartDate"),
        )
        job.record_identity = f"deutsche_bank:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https"
                or entry_parts.hostname not in {"careers.db.com", "www.careers.db.com"}
                or entry_parts.path.rstrip("/") != "/professionals/search-roles"):
            raise ValueError("Deutsche Bank dedicated flow must start from its official HTTPS professionals entry")

        component_started = time.perf_counter()
        entry = await self.http.get(entry_url)
        self._assert_careers_url(str(entry.url))
        soup = BeautifulSoup(entry.text, "lxml")
        language_code = str(soup.html.get("lang") or "").strip() if soup.html else ""
        if not language_code:
            raise ValueError("Deutsche Bank entry page no longer exposes its document language")
        modules = soup.select("#job-module[data-jobmodule]")
        if len(modules) != 1 or modules[0].get("data-jobmodule") != "PROFESSIONAL":
            raise ValueError("Deutsche Bank entry page no longer mounts the PROFESSIONAL job module")
        careers_srcs = [node.get("src") for node in soup.find_all("script", src=True)
                        if node.get("src") and "careersJs" in node.get("src")]
        if len(careers_srcs) != 1:
            raise ValueError("Deutsche Bank entry page no longer ships exactly one careersJs script")
        careers_url, careers_script = await self._company_script(str(entry.url), careers_srcs[0], "careersJs")
        if "jobplatform.js" not in careers_script or 'document.querySelector("#job-module")' not in careers_script:
            raise ValueError("Deutsche Bank careersJs no longer attaches jobplatform.js to #job-module")
        jobplatform_srcs = re.findall(r'setAttribute\("src","([^"]*jobplatform\.js[^"]*)"\)', careers_script)
        if len(jobplatform_srcs) != 1:
            raise ValueError("Deutsche Bank careersJs no longer exposes exactly one jobplatform script")
        jobplatform_url, jobplatform_script = await self._company_script(
            str(entry.url), jobplatform_srcs[0], "jobplatform"
        )
        search_base, jobhtml_base, descriptors, sort_criteria = self._jobplatform_source(jobplatform_script)
        component_seconds = time.perf_counter() - component_started

        # Expanding-window pagination, exactly as the module's Load More
        # control: CountItem 10, 20, 30, ... with FirstItem fixed at 1.
        listing_started = time.perf_counter()
        count_item = 10
        total = None
        ordered_ids = []
        first_page_ids = []
        by_id = {}
        page_reconciliations = []
        changed_membership = False
        final_snapshot_verified = False
        expansion_complete = False
        final_payload = {}
        while True:
            if not self.page_guard():
                self.result.errors.append("Deutsche Bank pagination exceeded the page budget before completion")
                break
            payload, counts, rows = await self._search_snapshot(
                search_base, descriptors, sort_criteria, language_code, count_item, "initial" if total is None else "expansion"
            )
            page_total = counts["SearchResultCountAll"]
            if total is None:
                total = page_total
            elif page_total != total:
                changed_membership = True
                self.result.errors.append("Deutsche Bank SearchResultCountAll changed during expanding-window collection")
            ids = [identity for identity, _, _ in rows]
            if ids[:len(ordered_ids)] != ordered_ids:
                changed_membership = True
                self.result.errors.append("Deutsche Bank expanding-window order changed during collection")
            if not first_page_ids:
                first_page_ids = list(ids)
            if len(ids) > len(ordered_ids):
                ordered_ids = list(ids)
                for identity, descriptor, item in rows:
                    by_id[identity] = (descriptor, item)
            page_reconciliations.append({
                "CountItem": count_item,
                "SearchResultCount": counts["SearchResultCount"],
                "SearchResultCountAll": counts["SearchResultCountAll"],
                "returned_items": len(rows),
            })
            final_payload = payload
            if counts["SearchResultCount"] >= counts["SearchResultCountAll"]:
                expansion_complete = True
                break
            count_item += 10

        # One confirmation re-request of the full window, mirroring a final
        # published-membership snapshot.
        if expansion_complete:
            try:
                final_payload, final_counts, final_rows = await self._search_snapshot(
                    search_base, descriptors, sort_criteria, language_code, count_item, "final"
                )
                if not self.page_guard():
                    self.result.errors.append("Deutsche Bank final snapshot exceeded the page budget")
                final_ids = [identity for identity, _, _ in final_rows]
                if final_ids != ordered_ids or final_counts["SearchResultCountAll"] != total:
                    changed_membership = True
                    self.result.errors.append("Deutsche Bank final full-window snapshot differs from the expansion")
                else:
                    final_snapshot_verified = True
            except Exception as exc:
                self.result.errors.append(
                    f"Deutsche Bank final full-window snapshot failed; expansion rows retained: {type(exc).__name__}: {exc}"
                )
        listing_seconds = time.perf_counter() - listing_started

        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["component_ui_phase_seconds"] = round(component_seconds, 6)
        base_metadata = {
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "careers_js_url": careers_url,
            "jobplatform_js_url": jobplatform_url,
            "search_base_url": search_base,
            "jobhtml_base_url": jobhtml_base,
            "matched_object_descriptors": descriptors,
            "search_sort": sort_criteria,
            "language_code": language_code,
            "membership_authority": (
                "Deutsche Bank PROFESSIONAL job-module embedded by its official Search Roles page; "
                "the exact unfiltered Beesite professional-search request (SearchCriteria []), "
                "expanded CountItem 10, 20, ... to SearchResultCountAll"
            ),
            "listing_filter_behavior": (
                "Unfiltered module state: SearchCriteria is empty. Division/profession/country and "
                "keyword controls only narrow the same endpoint; collecting all public postings "
                "requires no filters."
            ),
            "ui_pagination": "Expanding window: CountItem grows by 10 per Load More until SearchResultCount reaches SearchResultCountAll; FirstItem stays 1.",
            "page_reconciliations": page_reconciliations,
            "final_snapshot_verified": final_snapshot_verified,
            "membership_changed_during_collection": changed_membership,
            "initial_visible_ids": first_page_ids,
            "final_visible_ids": ordered_ids,
            "search_requests": len(page_reconciliations) + (1 if expansion_complete else 0),
        }
        self.result.metadata.update(base_metadata)

        if not ordered_ids:
            self.result.expected_total = 0
            self.result.errors.append(
                "Deutsche Bank official professional search returned zero jobs; empty state requires independent review"
            )
            return self.result

        first_identity = ordered_ids[0]
        representative_error = None
        representative_started = time.perf_counter()
        try:
            response = await self.http.get(jobhtml_url(jobhtml_base, first_identity))
            if str(response.url) != jobhtml_url(jobhtml_base, first_identity):
                raise ValueError("Deutsche Bank representative jobhtml request redirected")
            self._parse_detail(response.json(), first_identity)
        except Exception as exc:
            representative_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(
                f"Deutsche Bank representative exact detail {jobhtml_url(jobhtml_base, first_identity)}: {representative_error}"
            )
        representative_seconds = time.perf_counter() - representative_started

        placeholders = []
        for identity in ordered_ids:
            descriptor, item = by_id[identity]
            placeholders.append(self._placeholder(
                identity, descriptor, item, entry_url=str(entry.url), search_base=search_base,
                jobhtml_base=jobhtml_base, language_code=language_code,
            ))

        async def detail(job):
            job_id = job.job_id
            response = await self.http.get(jobhtml_url(jobhtml_base, job_id))
            if str(response.url) != jobhtml_url(jobhtml_base, job_id):
                raise ValueError("Deutsche Bank jobhtml request redirected")
            descriptor, item = by_id[job_id]
            return self._make_job(
                job_id, descriptor, item, self._parse_detail(response.json(), job_id),
                entry_url=str(entry.url), search_base=search_base, jobhtml_base=jobhtml_base,
                language_code=language_code,
            )

        # Each stored record carries its own archived detail response; the
        # earlier representative open is timed evidence, retained separately.
        hydrated = await self.hydrate(placeholders, detail)
        final_ids = {identity for identity, _, _ in self._listing_rows(final_payload)[1]} \
            if final_payload else set()
        for job in hydrated:
            if job.job_id not in final_ids:
                job.warnings.append("membership_changed_during_collection")
            self.add(job)

        self.result.expected_total = total
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        facets = {}
        facets_source = final_payload.get("SearchResult", {}).get("UserArea", {}).get("Facets")
        if isinstance(facets_source, dict):
            facets = {key: value.get("CountTotal") if isinstance(value, dict) else None
                      for key, value in facets_source.items()}
        self.result.metadata.update({
            "facet_count_totals": facets,
            "representative_detail_url": jobhtml_url(jobhtml_base, first_identity),
            "representative_detail_opened": representative_error is None,
            "representative_detail_error": representative_error,
            "description_source": (
                "Each exact listed PositionID's jobhtml JSON html and apply_uri; the native "
                "JobPage renders that html with two apply buttons pointing at the same apply_uri."
            ),
            "source_has_structured_salary_field": False,
            "salary_policy": (
                "No structured salary field is published on listing or detail; prose benefit "
                "mentions are retained in full text and salary stays null."
            ),
            "job_attachment_links": {
                job.job_id: job.raw.get("source_ui", {}).get("attachment_links", [])
                for job in hydrated if job.raw.get("source_ui", {}).get("attachment_links")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "detail_requests_including_representative": len(ordered_ids) + 1,
        })
        self.result.timings["representative_detail_ui_phase_seconds"] = round(representative_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result


__all__ = ["DeutscheBank", "native_job_url", "jobhtml_url", "detail_h1_title"]
