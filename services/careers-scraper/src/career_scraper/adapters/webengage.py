"""WebEngage's own /careers/ -> /current-openings/ page and its embedded Keka listing.

Observed journey (2026-11-08):

    https://webengage.com/careers/            301 -> /current-openings/
    https://webengage.com/current-openings/   inline window.khConfig + embedjobs script
    https://webklipper.keka.com/careers/api/embedjobs/default/active/<identifier>
                                              company-scoped membership (26 rows)
    https://webklipper.keka.com/careers/jobdetails/<id>   server-rendered full detail
    https://webklipper.keka.com/careers/applyjob/<id>     exact application route

The `active` response is the unfiltered membership; the widget only filters it
client-side by department/location and has no pagination.
"""
import hashlib
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

IDENTIFIER = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
KH_CONFIG = re.compile(r"window\.khConfig\s*=\s*\{(.*?)\}", re.S)
EMBED_SCRIPT = re.compile(
    r"""<script[^>]*\bsrc=["']([^"']*api/embedjobs/js/[0-9a-f-]+)["']""", re.I
)
JOB_TYPE_LABELS = {1: "Part Time", 2: "Full Time"}
SALARY_PERIOD_LABELS = {0: "Not Available", 1: "Hourly", 2: "Bi Weekly", 3: "Monthly", 4: "Annual"}


def compact_body_hash(html):
    """Whitespace-free NFC hash of the plain-text body (audit comparison helper)."""
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(domain, identity):
    if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
        raise ValueError(f"WebEngage listing job ID is not a positive integer: {identity!r}")
    return urljoin(domain, f"jobdetails/{identity}")


def apply_job_url(domain, identity):
    if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
        raise ValueError(f"WebEngage listing job ID is not a positive integer: {identity!r}")
    return urljoin(domain, f"applyjob/{identity}")


def job_type_label(value):
    return JOB_TYPE_LABELS.get(value, value)


def experience_label(value):
    """Mirror the widget's shipped rule: a bare positive number renders as 'N year(s)'."""
    if not isinstance(value, str):
        return value
    if re.fullmatch(r"[0-9]+[.]?[0-9]*[ ]*\+?", value.strip()) and float(value) > 0:
        return f"{value.strip()} " + ("year" if float(value) == 1 else "years")
    return value


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
    return {
        "source_text": source_text,
        "range": salary_range,
        "salary_period": period_label,
        "salary_period_code": (salary_range or {}).get("salaryPeriod") if isinstance(salary_range, dict) else None,
    }


def attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class WebEngage(Adapter):
    """Follow only WebEngage's observed official entry and Keka-embedded listing."""

    VERIFICATION_REVISION = "webengage.official-current-openings-keka-embed.1"
    ENTRY_HOSTS = {"webengage.com", "www.webengage.com"}
    KEKA_HOST = "webklipper.keka.com"
    EXPECTED_IDENTIFIER = "db770a22-93d1-46f6-af73-c35e960ea796"
    EXPECTED_ENTRY_PATH = "/careers/"
    EXPECTED_FINAL_PATH = "/current-openings/"

    @classmethod
    def _kh_config(cls, html):
        matches = KH_CONFIG.findall(html)
        if len(matches) != 1:
            raise ValueError(f"WebEngage entry page exposes {len(matches)} window.khConfig blocks, expected 1")
        body = matches[0]
        identifier = re.search(r"identifier:\s*['\"]([^'\"]+)['\"]", body)
        domain = re.search(r"domain:\s*['\"]([^'\"]+)['\"]", body)
        target = re.search(r"targetContainer:\s*['\"]([^'\"]+)['\"]", body)
        if not identifier or not domain:
            raise ValueError("WebEngage khConfig is missing identifier/domain")
        ident = identifier.group(1).strip()
        dom = domain.group(1).strip()
        if not IDENTIFIER.fullmatch(ident):
            raise ValueError(f"WebEngage khConfig identifier is not a UUID: {ident!r}")
        if ident != cls.EXPECTED_IDENTIFIER:
            raise ValueError(f"WebEngage khConfig identifier changed from the inspected tenant: {ident}")
        parsed = urlsplit(dom)
        if (parsed.scheme != "https" or parsed.hostname != cls.KEKA_HOST
                or parsed.path.rstrip("/") != "/careers"):
            raise ValueError(f"WebEngage khConfig domain is not the observed Keka careers domain: {dom}")
        if not dom.endswith("/"):
            dom += "/"
        container = target.group(1).strip() if target else None
        if container != "#khembedjobs":
            raise ValueError(f"WebEngage khConfig targetContainer changed from #khembedjobs: {container!r}")
        return {"identifier": ident, "domain": dom, "targetContainer": container, "portalName": "default"}

    @classmethod
    def _embed_script_url(cls, entry_url, html, identifier):
        matches = sorted({urljoin(entry_url, m) for m in EMBED_SCRIPT.findall(html)})
        expected = f"https://{cls.KEKA_HOST}/careers/api/embedjobs/js/{identifier}"
        if matches != [expected]:
            raise ValueError(f"WebEngage entry page does not expose exactly one embedjobs script for {identifier}: {matches}")
        return matches[0]

    @staticmethod
    def _assert_embedjobs_script(script, identifier):
        required = (
            "api/embedjobs/grouplinkstatus/",
            "api/embedjobs/${portalName}/active/",
            "api/organization/${portalName}/careerportalinfo",
            "api/embedjobs/departments/",
            "api/embedjobs/businessunits/",
            "api/embedjobs/globalfiltergroups/",
            "jobdetails/' + job.id",
            "khConfig.identifier",
        )
        missing = [token for token in required if token not in script]
        if missing:
            raise ValueError(f"WebEngage embedjobs script no longer matches the observed listing flow; missing {missing}")
        if script.count("api/embedjobs/${portalName}/active/") != 1:
            raise ValueError("WebEngage embedjobs script exposes an unexpected number of active-job fetch templates")

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, list):
            raise ValueError(f"WebEngage {snapshot_name} listing response is not a jobs array")
        rows = payload
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0 for identity in ids):
            raise ValueError(f"WebEngage {snapshot_name} listing contains a missing or invalid exact job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"WebEngage {snapshot_name} listing repeats a company-published ID")
        if any(not isinstance(row.get("title"), str) or not row["title"].strip() for row in rows):
            raise ValueError(f"WebEngage {snapshot_name} listing contains an unidentified job")
        for row in rows:
            if not isinstance(row.get("description"), str) or not row["description"].strip():
                raise ValueError(f"WebEngage {snapshot_name} job {row.get('id')} is missing its description field")
            if len(text_html(row["description"]).strip()) < 100:
                raise ValueError(f"WebEngage {snapshot_name} job {row.get('id')} description is implausibly short")
        return rows

    @staticmethod
    def _ui_groups(rows):
        """Mirror bindJobs(): group by departmentName, default JS string sort of group keys."""
        groups = {}
        for row in rows:
            label = row.get("departmentName") or "Other Jobs"
            groups.setdefault(label, []).append(row)
        ordered = {key: groups[key] for key in sorted(groups.keys())}
        return {key: [row["id"] for row in value] for key, value in ordered.items()}

    def _make_job(self, row, *, domain, active_url, portal_name):
        identity = row["id"]
        job_url = native_job_url(domain, identity)
        job_apply_url = apply_job_url(domain, identity)
        description_html = row.get("description") or ""
        description_text = text_html(description_html)
        salary = salary_from_row(row)
        links = attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_job_url": job_url,
                "apply_url": job_apply_url,
                "listing_api_url": active_url,
                "job_card_href_rule": domain + "jobdetails/" + str(identity),
                "rendered_job_type": job_type_label(row.get("jobType")),
                "rendered_experience": experience_label(row.get("experience")),
                "rendered_salary": row.get("salaryRangeFormat") or None,
                "attachment_links": links,
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=job_url,
            job_id=str(identity),
            requisition_id=None,
            apply_url=job_apply_url,
            description=description_html,
            method="webengage.keka_embed.active.description",
            description_url=active_url,
            locations=list(row.get("jobLocations") or []),
            department=row.get("departmentName"),
            employment_type=job_type_label(row.get("jobType")),
            date_posted=row.get("publishedOn") or None,
            date_posted_raw=row.get("publishedOn"),
            date_updated=None,
            salary=salary,
            experience=row.get("experience"),
            employer=portal_name,
        )
        job.description_text = description_text
        job.record_identity = f"webengage:{identity}"
        return job

    async def _json_snapshot(self, url, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"WebEngage {label} request redirected away from its inspected source: {url} -> {response.url}")
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"WebEngage {label} response is not JSON") from exc

    async def _verify_detail(self, url, row, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"WebEngage {label} detail route redirected to a different job ID or page")
        soup = BeautifulSoup(response.text, "lxml")
        h1 = soup.select_one("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
        if title != row["title"]:
            raise ValueError(f"WebEngage {label} detail <h1> title differs from the listed title")
        desc = soup.select_one(".job-description-container")
        if desc is None or not desc.decode_contents().strip():
            raise ValueError(f"WebEngage {label} detail page has no server-rendered description body")
        if text_html(desc.decode_contents()) != text_html(row["description"]):
            raise ValueError(f"WebEngage {label} detail body text differs from the listed description text")
        if soup.select_one("#apply-job") is None:
            raise ValueError(f"WebEngage {label} detail page has no #apply-job action")
        return {"url": url, "title": title, "description_sha256": compact_body_hash(desc.decode_contents())}

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in self.ENTRY_HOSTS
                or parts.path.rstrip("/") != self.EXPECTED_ENTRY_PATH.rstrip("/")):
            raise ValueError("WebEngage dedicated flow must start from its official HTTPS /careers/ entry")
        entry = await self.http.get(entry_url)
        final = urlsplit(str(entry.url))
        if final.hostname not in self.ENTRY_HOSTS or final.path != self.EXPECTED_FINAL_PATH:
            raise ValueError(f"WebEngage /careers/ no longer redirects to its observed /current-openings/ page: {entry.url}")
        entry_html = entry.text
        kh = self._kh_config(entry_html)
        identifier = kh["identifier"]
        domain = kh["domain"]
        if 'id="khembedjobs"' not in entry_html:
            raise ValueError("WebEngage entry page no longer contains the #khembedjobs target container")
        embedjobs_js_url = self._embed_script_url(str(entry.url), entry_html, identifier)
        embedjobs_page = await self.http.get(embedjobs_js_url)
        if str(embedjobs_page.url) != embedjobs_js_url:
            raise ValueError("WebEngage embedjobs script redirected away from its inspected source")
        self._assert_embedjobs_script(embedjobs_page.text, identifier)

        grouplink_url = urljoin(domain, f"api/embedjobs/grouplinkstatus/{identifier}")
        portalinfo_url = urljoin(domain, "api/organization/default/careerportalinfo")
        departments_url = urljoin(domain, f"api/embedjobs/departments/{identifier}")
        filters_url = urljoin(domain, f"api/embedjobs/globalfiltergroups/{identifier}")
        businessunits_url = urljoin(domain, f"api/embedjobs/businessunits/{identifier}")
        active_url = urljoin(domain, f"api/embedjobs/default/active/{identifier}")

        group_payload = await self._json_snapshot(grouplink_url, "group-link status")
        if not isinstance(group_payload, dict) or "isGroupLinked" not in group_payload:
            raise ValueError("WebEngage group-link status response has no isGroupLinked field")
        portal_info = await self._json_snapshot(portalinfo_url, "career portal info")
        if not isinstance(portal_info, dict) or portal_info.get("shortName") != "WebEngage":
            raise ValueError("WebEngage career portal info does not identify WebEngage")
        portal_name = portal_info.get("name") or "WebEngage"
        departments_payload = await self._json_snapshot(departments_url, "departments")
        filters_payload = await self._json_snapshot(filters_url, "global filter groups")
        businessunits_payload = await self._json_snapshot(businessunits_url, "business units")
        if not isinstance(departments_payload, list) or not isinstance(filters_payload, list):
            raise ValueError("WebEngage department/filter responses are not lists")
        if not isinstance(businessunits_payload, list):
            raise ValueError("WebEngage business units response is not a list")

        initial_payload = await self._json_snapshot(active_url, "initial")
        initial_rows = self._rows(initial_payload, "initial")
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "WebEngage official embedded listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result
        initial_by_id = {row["id"]: row for row in initial_rows}

        detail_started = time.perf_counter()
        representative = []
        for label, identity in (("first", initial_rows[0]["id"]), ("last", initial_rows[-1]["id"])):
            url = native_job_url(domain, identity)
            try:
                representative.append({"position": label, **await self._verify_detail(url, initial_by_id[identity], label)})
            except Exception as exc:
                self.result.errors.append(
                    f"WebEngage representative exact detail {url}: {type(exc).__name__}: {exc}"
                )
        representative_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_payload = await self._json_snapshot(active_url, "final")
            final_rows = self._rows(final_payload, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "WebEngage final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {row["id"]: row for row in final_rows}
        changed_membership = final_snapshot_verified and set(initial_by_id) != set(final_by_id)
        if changed_membership:
            self.result.errors.append("webengage_official_listing_membership_changed_between_initial_and_final_snapshots")
        changed_rows = sorted(
            identity for identity in final_by_id.keys() & initial_by_id.keys()
            if final_by_id[identity] != initial_by_id[identity]
        )

        mapping_started = time.perf_counter()
        ordered_ids = [row["id"] for row in final_rows] + [
            row["id"] for row in initial_rows if row["id"] not in final_by_id
        ]
        for identity in ordered_ids:
            row = final_by_id.get(identity) or initial_by_id[identity]
            job = self._make_job(row, domain=domain, active_url=active_url, portal_name=portal_name)
            if identity not in final_by_id:
                job.warnings.append("removed_from_listing_before_final_snapshot")
            self.add(job)
        mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        self.result.listing_complete = not changed_membership and not self.result.errors

        listing_setting = portal_info.get("jobListingSetting") or {}
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "official_entry_final_url": str(entry.url),
            "official_entry_redirect_chain": [
                {"status": hop.status_code, "url": str(hop.url), "location": hop.headers.get("location")}
                for hop in entry.history
            ],
            "keka_careers_domain": domain,
            "keka_portal_identifier": identifier,
            "kh_target_container": kh["targetContainer"],
            "embedjobs_js_url": embedjobs_js_url,
            "grouplinkstatus_url": grouplink_url,
            "is_group_linked": bool(group_payload.get("isGroupLinked")),
            "careerportalinfo_url": portalinfo_url,
            "portal_short_name": portal_info.get("shortName"),
            "portal_name": portal_name,
            "departments_url": departments_url,
            "departments_count": len(departments_payload),
            "globalfiltergroups_url": filters_url,
            "globalfiltergroups_count": len(filters_payload),
            "businessunits_url": businessunits_url,
            "businessunits_count": len(businessunits_payload),
            "active_url": active_url,
            "membership_authority": (
                "WebEngage's official /careers/ 301-redirects to /current-openings/, which publishes the exact "
                "webklipper.keka.com khConfig (identifier + domain) and embedjobs script. The unfiltered "
                "api/embedjobs/default/active/<identifier> response is the company-visible membership; it has no "
                "pagination and filters are client-side only."
            ),
            "listing_filter_behavior": (
                "Unfiltered active rows. careerportalinfo.jobListingSetting filters department+location and job "
                "fields location/experience/jobType are applied in the browser only."
            ),
            "job_listing_setting": listing_setting,
            "ui_pagination": (
                "No server pagination and no load-more control. The widget groups all active rows by departmentName "
                "(default JS string sort of group keys) with a job-count badge per group."
            ),
            "ui_group_counts": {key: len(value) for key, value in self._ui_groups(final_rows).items()},
            "initial_api_rows": len(initial_payload),
            "final_api_rows": len(final_payload),
            "final_snapshot_verified": final_snapshot_verified,
            "initial_ids": [row["id"] for row in initial_rows],
            "final_ids": [row["id"] for row in final_rows],
            "source_rows_changed_between_snapshots": changed_rows,
            "representative_detail_checks": representative,
            "representative_detail_error_count": sum(
                1 for error in self.result.errors if "representative exact detail" in error
            ),
            "description_source": (
                "Each exact active row's full description HTML; representative exact jobdetails pages server-render "
                "the same body in .job-description-container (all 26 independently audited by companies/webengage/run.py)."
            ),
            "apply_url_rule": (
                "Shipped app.min.js getApplyJobUrl() resolves applyjob/<id> against the careers origin; no "
                "application form is submitted."
            ),
            "job_type_labels": {str(k): v for k, v in JOB_TYPE_LABELS.items()},
            "salary_policy": (
                "salaryRange object and salaryRangeFormat source text retained verbatim with salaryPeriod "
                "code/label; no conversion or invented range."
            ),
            "requisition_policy": "The Keka listing publishes no requisition number; requisition_id stays null.",
            "job_attachment_links": {
                row["id"]: attachment_links(row.get("description")) for row in final_rows
                if attachment_links(row.get("description"))
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "listing_requests": 2,
            "representative_detail_requests": len(representative),
        })
        self.result.timings["representative_detail_phase_seconds"] = round(representative_seconds, 6)
        self.result.timings["job_mapping_seconds"] = round(mapping_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
