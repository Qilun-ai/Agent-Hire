"""ConveGenius's own careers.html -> Keka hosted listing -> exact job details journey."""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

IDENTIFIER = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
PORTAL_FETCH = re.compile(
    r"fetch\s*\(\s*(['\"])(/ats/documents/[0-9a-f-]+/careerportal/[0-9a-f]+\.html)\1\s*\)"
)
KH_CONFIG = re.compile(r"window\.khConfig\s*=\s*\{([^}]+)\}")
JOB_TYPE_LABELS = {1: "Part Time", 2: "Full Time"}
SALARY_PERIOD_LABELS = {0: "Not Available", 1: "Hourly", 2: "Bi Weekly", 3: "Monthly", 4: "Annual"}


def compact_body_hash(html):
    """NFC/whitespace-free body hash of the plain-text body (for audit comparison)."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(domain, identity):
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"ConveGenius official listing job ID is not a positive integer: {identity!r}")
    return urljoin(domain, f"jobdetails/{identity}")


def apply_job_url(domain, identity):
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"ConveGenius official listing job ID is not a positive integer: {identity!r}")
    return urljoin(domain, f"applyjob/{identity}")


def job_type_label(value):
    return JOB_TYPE_LABELS.get(value, value)


def salary_from_row(row):
    salary_range = row.get("salaryRange")
    source_text = row.get("salaryRangeFormat")
    if salary_range is None and not source_text:
        return None
    if isinstance(salary_range, dict):
        period = salary_range.get("salaryPeriod")
        period_label = SALARY_PERIOD_LABELS.get(period) if period is not None else None
    else:
        period_label = None
    return {"source_text": source_text, "range": salary_range, "salary_period": period_label,
            "salary_period_code": (salary_range or {}).get("salaryPeriod") if isinstance(salary_range, dict) else None}


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class ConveGenius(Adapter):
    """Follow only ConveGenius's observed official entry and Keka-hosted journey."""

    VERIFICATION_REVISION = "convegenius.official-careers-keka-embed-active-details.1"
    ENTRY_HOST = "convegenius.com"
    KEKA_HOST = "convegenius.keka.com"
    EXPECTED_IDENTIFIER = "0c764aaa-3be8-4767-b051-a188dd5c0cb6"

    @staticmethod
    def _keka_careers_url(entry_url, html):
        soup = BeautifulSoup(html, "lxml")
        hrefs = sorted({a.get("href", "").strip() for a in soup.select("a[href]")})
        matches = [h for h in hrefs if h.rstrip("/") == "https://convegenius.keka.com/careers"]
        if len(matches) != 1:
            raise ValueError(
                f"ConveGenius official entry does not expose exactly one Keka careers link ({len(matches)})"
            )
        return matches[0]

    @staticmethod
    def _portal_document_url(keka_url, html):
        matches = PORTAL_FETCH.findall(html)
        # findall returns list of tuples due to capture group; normalize
        paths = sorted({m[1] if isinstance(m, tuple) else m for m in matches})
        if len(paths) != 1:
            raise ValueError(f"Keka careers landing does not expose exactly one careerportal fetch ({len(paths)})")
        return urljoin(keka_url, paths[0])

    @staticmethod
    def _kh_config(portal_html):
        match = KH_CONFIG.search(portal_html)
        if not match:
            raise ValueError("ConveGenius careerportal document does not expose window.khConfig")
        body = match.group(1)
        identifier = re.search(r"identifier:\s*'([^']+)'", body)
        domain = re.search(r"domain:\s*'([^']+)'", body)
        target = re.search(r"targetContainer:\s*'([^']+)'", body)
        if not identifier or not domain:
            raise ValueError("ConveGenius khConfig is missing identifier/domain")
        ident = identifier.group(1).strip()
        dom = domain.group(1).strip()
        if not IDENTIFIER.fullmatch(ident):
            raise ValueError(f"ConveGenius khConfig identifier is not a UUID: {ident!r}")
        if ident != ConveGenius.EXPECTED_IDENTIFIER:
            raise ValueError(f"ConveGenius khConfig identifier changed from inspected tenant: {ident}")
        parsed = urlsplit(dom)
        if parsed.scheme != "https" or parsed.hostname != ConveGenius.KEKA_HOST or parsed.path.rstrip("/") != "/careers":
            raise ValueError(f"ConveGenius khConfig domain is not the observed Keka careers domain: {dom}")
        if not dom.endswith("/"):
            dom = dom + "/"
        return {"identifier": ident, "domain": dom, "targetContainer": target.group(1) if target else "#khembedjobs",
                "portalName": None}

    @staticmethod
    def _assert_embedjobs_script(script, identifier):
        required = (
            "api/embedjobs/grouplinkstatus/",
            "api/embedjobs/${portalName}/active/",
            "api/organization/${portalName}/careerportalinfo",
            "api/embedjobs/departments/",
            "jobdetails/' + job.id",
            "khConfig.identifier",
        )
        missing = [token for token in required if token not in script]
        if missing:
            raise ValueError(f"ConveGenius embedjobs script no longer matches observed listing flow; missing {missing}")
        # Ensure no second active endpoint / board confusion: exactly one active fetch template
        if script.count("api/embedjobs/${portalName}/active/") != 1:
            raise ValueError("ConveGenius embedjobs script exposes an unexpected number of active-job fetch templates")

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, list):
            raise ValueError(f"ConveGenius {snapshot_name} listing response is not a jobs array")
        rows = payload
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, int) or identity <= 0 for identity in ids):
            raise ValueError(f"ConveGenius {snapshot_name} listing contains a missing or invalid exact job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"ConveGenius {snapshot_name} listing repeats a company-published ID")
        if any(not row.get("title") for row in rows):
            raise ValueError(f"ConveGenius {snapshot_name} listing contains an unidentified job")
        for row in rows:
            if not isinstance(row.get("jobNumber"), str) or not row["jobNumber"].strip():
                raise ValueError(f"ConveGenius {snapshot_name} job {row.get('id')} is missing jobNumber")
            if not isinstance(row.get("description"), str):
                raise ValueError(f"ConveGenius {snapshot_name} job {row.get('id')} is missing its description field")
        return rows

    @staticmethod
    def _ui_order(rows):
        # Mirror bindJobs: groups sorted alphabetically by departmentName,
        # jobs inside each group sorted by title (case-insensitive).
        def group_label(row):
            return row.get("departmentName") or "Other Jobs"

        groups = {}
        for row in rows:
            groups.setdefault(group_label(row), []).append(row)
        ordered = []
        for key in sorted(groups.keys()):
            ordered.extend(sorted(groups[key], key=lambda r: (r.get("title") or "").lower()))
        return ordered

    def _make_job(self, row, *, domain, active_url, initial_row=None, membership_changed=False):
        identity = row["id"]
        job_url = native_job_url(domain, identity)
        apply_url = apply_job_url(domain, identity)
        description_html = row.get("description") or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: ConveGenius public detail description is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: ConveGenius public detail description is implausibly short")
        date_posted = row.get("publishedOn") if row.get("publishedOn") else None
        salary = salary_from_row(row)
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": job_url,
                "apply_url": apply_url,
                "rendered_job_type": job_type_label(row.get("jobType")),
                "listing_api_url": active_url,
                "job_card_href_rule": domain + "jobdetails/" + str(identity),
                "attachment_links": attachment_links,
                "salary_from_description": salary,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=job_url,
            job_id=str(identity),
            requisition_id=row.get("jobNumber"),
            apply_url=apply_url,
            description=description_html,
            method="convegenius.keka_embed.active.description",
            description_url=active_url,
            locations=list(row.get("jobLocations") or []),
            department=row.get("departmentName"),
            employment_type=job_type_label(row.get("jobType")),
            date_posted=date_posted,
            date_posted_raw=row.get("publishedOn"),
            date_updated=None,
            salary=salary,
            experience=row.get("experience"),
        )
        job.description_text = description_text
        job.record_identity = f"convegenius:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def _json_snapshot(self, url, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"ConveGenius {label} request redirected away from its inspected source: {url} -> {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"ConveGenius {label} response is not JSON") from exc
        return payload

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in {"convegenius.com", "www.convegenius.com"}
                or entry_parts.path != "/careers.html"):
            raise ValueError("ConveGenius dedicated flow must start from its official HTTPS /careers.html entry")
        entry = await self.http.get(entry_url)
        if urlsplit(str(entry.url)).hostname not in {"convegenius.com", "www.convegenius.com"}:
            raise ValueError("ConveGenius official entry redirected away from its official site")
        keka_url = self._keka_careers_url(str(entry.url), entry.text)

        keka_page = await self.http.get(keka_url)
        if str(keka_page.url).rstrip("/") != keka_url.rstrip("/"):
            raise ValueError("ConveGenius Keka careers landing redirected to a different URL")
        portal_doc_url = self._portal_document_url(keka_url, keka_page.text)
        portal_parsed = urlsplit(portal_doc_url)
        if (portal_parsed.scheme != "https" or portal_parsed.hostname != self.KEKA_HOST
                or not portal_parsed.path.startswith("/ats/documents/")):
            raise ValueError(f"ConveGenius careerportal document left the observed Keka host: {portal_doc_url}")
        portal_page = await self.http.get(portal_doc_url)
        if str(portal_page.url) != portal_doc_url:
            raise ValueError("ConveGenius careerportal document redirected away from its inspected source")
        kh = self._kh_config(portal_page.text)
        identifier = kh["identifier"]
        domain = kh["domain"]
        embedjobs_js_url = urljoin(domain, f"api/embedjobs/js/{identifier}")
        embedjobs_page = await self.http.get(embedjobs_js_url)
        if str(embedjobs_page.url) != embedjobs_js_url:
            raise ValueError("ConveGenius embedjobs script redirected away from its inspected source")
        self._assert_embedjobs_script(embedjobs_page.text, identifier)

        grouplink_url = urljoin(domain, f"api/embedjobs/grouplinkstatus/{identifier}")
        portalinfo_url = urljoin(domain, "api/organization/default/careerportalinfo")
        active_url = urljoin(domain, f"api/embedjobs/default/active/{identifier}")

        group_payload = await self._json_snapshot(grouplink_url, "group-link status")
        if not isinstance(group_payload, dict) or "isGroupLinked" not in group_payload:
            raise ValueError("ConveGenius group-link status response has no isGroupLinked field")
        is_group_linked = bool(group_payload["isGroupLinked"])
        portal_info = await self._json_snapshot(portalinfo_url, "career portal info")
        if not isinstance(portal_info, dict) or portal_info.get("shortName") != "Convegenius":
            raise ValueError("ConveGenius career portal info does not identify Convegenius")
        departments_url = None
        departments_payload = None
        if not is_group_linked:
            departments_url = urljoin(domain, f"api/embedjobs/departments/{identifier}")
            departments_payload = await self._json_snapshot(departments_url, "departments")
            if not isinstance(departments_payload, list):
                raise ValueError("ConveGenius departments response is not a list")
        else:
            # Group-linked branch: fetch its filter-group membership endpoints for evidence.
            global_url = urljoin(domain, f"api/embedjobs/globalfiltergroups/{identifier}")
            bu_url = urljoin(domain, f"api/embedjobs/businessunits/{identifier}")
            global_payload = await self._json_snapshot(global_url, "global filter groups")
            bu_payload = await self._json_snapshot(bu_url, "business units")
            if not isinstance(global_payload, list) or not isinstance(bu_payload, list):
                raise ValueError("ConveGenius group-linked filter responses are not lists")

        initial_payload = await self._json_snapshot(active_url, "initial")
        initial_rows = self._rows(initial_payload, "initial")
        initial_by_id = {row["id"]: row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "ConveGenius official embedded listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result

        first_identity = self._ui_order(initial_rows)[0]["id"]
        exact_detail_url = native_job_url(domain, first_identity)
        detail_started = time.perf_counter()
        detail_error = None
        detail_verified = False
        detail_title = None
        try:
            detail_page = await self.http.get(exact_detail_url)
            if str(detail_page.url) != exact_detail_url:
                raise ValueError("ConveGenius exact detail route redirected to a different job ID or page")
            detail_soup = BeautifulSoup(detail_page.text, "lxml")
            h1 = detail_soup.select_one("h1")
            detail_title = h1.get_text(" ", strip=True) if h1 else None
            desc_node = detail_soup.select_one(".job-description-container")
            if detail_title != initial_by_id[first_identity]["title"]:
                raise ValueError("ConveGenius representative detail <h1> title differs from the listed title")
            if desc_node is None or not desc_node.decode_contents().strip():
                raise ValueError("ConveGenius representative detail page has no server-rendered description body")
            server_text = text_html(desc_node.decode_contents())
            expected_text = text_html(initial_by_id[first_identity]["description"])
            if server_text != expected_text:
                raise ValueError("ConveGenius representative detail body text differs from the listed description text")
            if detail_soup.select_one("#apply-job") is None:
                raise ValueError("ConveGenius representative detail page has no #apply-job action")
            detail_verified = True
        except Exception as exc:
            detail_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"ConveGenius representative exact detail {exact_detail_url}: {detail_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_payload = await self._json_snapshot(active_url, "final")
            final_rows = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "ConveGenius final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["id"]: row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("congenius_official_listing_membership_changed_between_initial_and_final_snapshots")

        ui_final_rows = self._ui_order(final_rows)
        ui_initial_rows = self._ui_order(initial_rows)
        all_ids = list(dict.fromkeys([row["id"] for row in ui_final_rows] + [row["id"] for row in ui_initial_rows]))
        changed_rows = [identity for identity in final_by_id.keys() & initial_by_id.keys()
                        if final_by_id[identity] != initial_by_id[identity]]
        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, domain=domain, active_url=active_url,
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        job_listing_setting = (portal_info.get("jobListingSetting") or {}) if isinstance(portal_info, dict) else {}
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "keka_careers_url": keka_url,
            "careerportal_document_url": portal_doc_url,
            "embedjobs_js_url": embedjobs_js_url,
            "portal_identifier": identifier,
            "kh_config_domain": domain,
            "kh_target_container": kh.get("targetContainer"),
            "portal_name_default": True,
            "grouplinkstatus_url": grouplink_url,
            "is_group_linked": is_group_linked,
            "careerportalinfo_url": portalinfo_url,
            "portal_short_name": portal_info.get("shortName") if isinstance(portal_info, dict) else None,
            "departments_url": departments_url,
            "departments_count": len(departments_payload) if isinstance(departments_payload, list) else None,
            "active_url": active_url,
            "membership_authority": (
                "ConveGenius official /careers.html links to convegenius.keka.com/careers; that landing fetches its "
                "careerportal document; khConfig domain+identifier drives the embedjobs script; the unfiltered "
                "api/embedjobs/default/active response is the company-visible membership. Filters (location, jobType "
                "from careerportalinfo.jobListingSetting) are client-side only."
            ),
            "listing_filter_behavior": "unfiltered active rows; client-side search + location/jobType filters per careerportalinfo.jobListingSetting",
            "job_listing_setting": job_listing_setting,
            "ui_pagination": "No server pagination; the widget renders all active rows grouped alphabetically by departmentName with jobs sorted by title.",
            "initial_api_rows": len(initial_payload),
            "final_api_rows": len(final_payload),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [row["id"] for row in ui_initial_rows],
            "final_visible_ids": [row["id"] for row in ui_final_rows],
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "exact_native_detail_page_opened": exact_detail_url,
            "representative_detail_title": detail_title,
            "representative_detail_server_body_matches_listing": detail_verified,
            "representative_detail_error": detail_error,
            "description_source": "Each exact active row's description HTML; the representative native detail page server-renders the same text in .job-description-container.",
            "apply_url_rule": "detail page <base href=\"/careers/\"> + shipped app.min.js getApplyJobUrl(): applyjob/<id> resolved against the careers origin; no application path was opened.",
            "salary_policy": "salaryRange object + salaryRangeFormat source text retained verbatim; salaryPeriod code/label retained; no conversion.",
            "job_attachment_links": {row["id"]: _job_attachment_links(row.get("description"))
                                     for row in final_rows if _job_attachment_links(row.get("description"))},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
