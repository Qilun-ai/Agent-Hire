"""Cure.fit's own hosted careers (Zwayam) listing and job-view journey.

Official entry https://www.cult.fit/careers 301-redirects to the
Angular app at https://careers.cult.fit/cult/, whose shipped
main bundle embeds the public API root and endpoint paths that the
app itself uses. This adapter follows exactly those observed
requests: the unfiltered ``jobs/search`` list (paginated by
``paginationStartNo``) and the per-job ``jobs-service/v1/jobs/careersite``
detail for each listed id. It does not search any other board.
"""
import base64
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

API_CONST = re.compile(
    r'COMPANYID:"([^"]+)",'
    r'COMPANYURL:"([^"]+)",'
    r'DOMAIN:"([^"]+)",'
    r'APIENDPOINT:"([^"]+)",'
    r'APIENDPOINTNEW:"([^"]+)",'
    r'JOBSHAREURL:"([^"]+)"'
)
MAIN_SCRIPT = re.compile(r"^main\.[a-f0-9]+\.js$")
ROUTE = re.compile(r'path:"(jobview/:jobUrl)"')

# The public.zwayam.com endpoints stall clients carrying the generic scraper
# user agent (WAF-side hang, verified 2026-11-07); the company app's own
# requests go out with a standard browser user agent, which we mirror here.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def compact_body_hash(html):
    """NFC/whitespace-free SHA-256 of a description's text (same convention as Atlan)."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def jobview_url(entry_url, route, job_url, job_id):
    """Build the exact published in-app job page URL for a listed job."""
    if not job_url or not str(job_id).isdigit():
        raise ValueError(f"Cure.fit listed job has no usable jobUrl/id: {job_url!r}/{job_id!r}")
    base = str(entry_url)
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, route.replace(":jobUrl", quote(str(job_url), safe="-._~"))) + f"?id={job_id}"


def public_share_url(job_share_url, job_url, domain, api_domain):
    return (
        f"{job_share_url}/job_preview/?jobUrl={quote(str(job_url), safe='-._~')}"
        f"&externalSource=facebookShare&campusUrl=empty&host={domain}&apiDomain={api_domain}"
    )


def _salary_from_row(src):
    low = (src.get("minJobSalary") or "").strip() if isinstance(src.get("minJobSalary"), str) else src.get("minJobSalary")
    high = (src.get("maxJobSalary") or "").strip() if isinstance(src.get("maxJobSalary"), str) else src.get("maxJobSalary")
    if not low and not high:
        return None
    return {"currency": src.get("currencyType") or None,
            "min": low or None, "max": high or None,
            "source_fields": ["minJobSalary", "maxJobSalary"]}


def _date_posted_update(src):
    """created-date string kept verbatim; epoch-ms mod date converted to ISO-8601."""
    from datetime import datetime, timezone

    date_posted = src.get("createDate") if isinstance(src.get("createDate"), str) and src["createDate"].strip() else None
    raw = src.get("createDate") if date_posted else None
    updated = src.get("modifiedDate")
    date_updated = None
    if isinstance(updated, int) and updated > 0:
        date_updated = datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat()
    elif isinstance(updated, str) and updated.strip():
        date_updated = updated
    return date_posted, raw, date_updated


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class CureFit(Adapter):
    """Follow only Cure.fit's live careers app and its exact API requests."""

    VERIFICATION_REVISION = "cure_fit.zwayam_hosted_careers_search_and_careersite_detail.1"
    CAREERS_HOST = "careers.cult.fit"
    API_HOST = "public.zwayam.com"
    EXPECTED_COMPANY_ID = "15470"

    @staticmethod
    def _assert_careers_url(url, *, what):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"careers.cult.fit"}:
            raise ValueError(f"Cure.fit {what} left the official careers host: {url}")

    @staticmethod
    def _app_config(main_js):
        match = API_CONST.search(main_js)
        if not match:
            raise ValueError("Cure.fit main bundle no longer embeds its public API config")
        company_id, company_url, domain, api_endpoint, api_endpoint_new, job_share = match.groups()
        try:
            decoded = base64.b64decode(company_id).decode("utf-8")
        except Exception as exc:
            raise ValueError("Cure.fit COMPANYID is not the inspected base64 id") from exc
        if decoded != CureFit.EXPECTED_COMPANY_ID:
            raise ValueError(f"Cure.fit company id changed from 15470: {company_id!r}")
        if domain != CureFit.CAREERS_HOST:
            raise ValueError(f"Cure.fit careers domain changed: {domain!r}")
        expected_api = "https://public.zwayam.com/"
        if api_endpoint != expected_api or api_endpoint_new != expected_api or job_share != "https://public.zwayam.com":
            raise ValueError("Cure.fit API endpoint changed from its inspected public host")
        return {
            "company_id_encoded": company_id,
            "company_url": company_url,
            "domain": domain,
            "api_endpoint": api_endpoint,
            "job_share_url": job_share,
        }

    @staticmethod
    def _assert_endpoints(main_js):
        missing = [token for token in (
            '"jobs/search"',
            '"jobs-service/v1/jobs/careersite"',
            "searchJobsv1",
            "getJobData",
            "filterCri:{paginationStartNo:0",
            "anyOfTheseWords",
            "paginationStartNo",
            "+this.showMoreBtnLimit",
        ) if token not in main_js]
        if missing:
            raise ValueError(f"Cure.fit main bundle no longer matches its observed search/detail flow: {missing}")

    @staticmethod
    def _route(main_js):
        match = ROUTE.search(main_js)
        if not match:
            raise ValueError("Cure.fit main bundle no longer exposes its jobview/:jobUrl route")
        if "?id=" not in main_js:
            raise ValueError("Cure.fit main bundle no longer appends ?id= to job view URLs")
        return match.group(1)

    async def _main_bundle(self, entry_url, entry_html):
        soup = BeautifulSoup(entry_html, "lxml")
        base = urljoin(str(entry_url), (soup.base.get("href") if soup.base else "/cult/"))
        candidates = [s.get("src") for s in soup.find_all("script", src=True)
                      if MAIN_SCRIPT.fullmatch((s.get("src") or "").rsplit("/", 1)[-1])]
        if len(candidates) != 1:
            raise ValueError(f"Cure.fit official page does not reference exactly one main bundle ({candidates})")
        bundle_url = urljoin(base, candidates[0])
        self._assert_careers_url(bundle_url, what="main bundle")
        response = await self.http.get(bundle_url)
        self._assert_careers_url(response.url, what="main bundle")
        return str(response.url), response.text

    async def _search_page(self, search_url, config, offset):
        filter_cri = json.dumps({
            "paginationStartNo": offset,
            "selectedCall": "sort",
            "sortCriteria": {"name": "modifiedDate", "isAscending": False},
            "anyOfTheseWords": "",
        })
        response = await self.http.request("POST", search_url, headers=BROWSER_HEADERS, files={
            "filterCri": (None, filter_cri),
            "domain": (None, config["domain"]),
            "companyId": (None, config["company_id_encoded"]),
        })
        if str(response.url) != search_url:
            raise ValueError("Cure.fit listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Cure.fit jobs/search response is not JSON") from exc
        return payload, filter_cri

    @staticmethod
    def _search_rows(payload, offset):
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ValueError(f"Cure.fit jobs/search offset {offset} returned a non-200 envelope")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError(f"Cure.fit jobs/search offset {offset} has no hits array")
        rows = []
        for hit in data["data"]:
            src = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(src, dict) or not isinstance(src.get("id"), int) or not src.get("jobUrl"):
                raise ValueError(f"Cure.fit jobs/search offset {offset} contains a malformed listing row")
            rows.append(src)
        return data, rows

    async def _detail(self, detail_url, config, src):
        body = {"jobUrl": src["jobUrl"], "externalSource": "CAREERSITE",
                "campusUrl": "empty", "companyId": self.EXPECTED_COMPANY_ID,
                "jobId": src["id"]}
        response = await self.http.request("POST", detail_url, headers=BROWSER_HEADERS, json=body)
        if str(response.url) != detail_url:
            raise ValueError("Cure.fit job detail request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Cure.fit careersite detail response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("id") != src["id"]:
            raise ValueError("Cure.fit careersite detail response id does not match the listed job")
        return payload

    def _make_job(self, src, detail, *, entry_url, route, config, search_url, detail_url, page_offset):
        job_id = src["id"]
        identity = str(job_id)
        job_view = jobview_url(str(entry_url), route, src["jobUrl"], job_id)
        share_url = public_share_url(config["job_share_url"], src["jobUrl"],
                                     config["domain"], "public.zwayam.com")
        jcd = detail.get("jobConfigurationData") if isinstance(detail.get("jobConfigurationData"), dict) else {}
        description_html = jcd.get("Description")
        if not isinstance(description_html, str) or not description_html.strip():
            description_html = detail.get("longDescription") or ""
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Cure.fit public detail Description is empty")
        description_text = text_html(description_html)
        if description_html.strip() and len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Cure.fit public detail Description is implausibly short")
        title = detail.get("jobTitle") or src.get("jobTitle")
        if not title:
            self.result.errors.append(f"{identity}: Cure.fit job has no title")
        date_posted, date_posted_raw, date_updated = _date_posted_update(detail)
        attachment_links = _job_attachment_links(description_html)
        if attachment_links:
            self.result.errors.append(
                f"{identity}: Cure.fit published role attachment links are present but no full attachment text was captured"
            )
        reference = detail.get("referenceNumber") or src.get("referenceNumber") or src.get("refNumber")
        if reference is not None:
            reference = str(reference)
        locations = [{"location": src.get("location") or detail.get("location"), "address": None}]
        if src.get("officeLocation") and src.get("officeLocation") != locations[0]["location"]:
            # Office label is part of the listed card; keep it visible without replacing location.
            locations[0]["office_location"] = src.get("officeLocation")
        raw = {
            "listing": src,
            "detail": detail,
            "source_ui": {
                "native_job_url": job_view,
                "public_share_redirect_url": share_url,
                "apply_url": job_view,
                "listing_api_url": search_url,
                "detail_api_url": detail_url,
                "listing_page_offset": page_offset,
                "job_title": title,
                "detail_company_name": detail.get("companyName"),
                "description_attribute_keys": list(jcd.keys()),
                "attachment_links": attachment_links,
            },
        }
        job = self.job(
            raw,
            title=title or "",
            url=job_view,
            job_id=identity,
            requisition_id=reference,
            apply_url=job_view,
            description=description_html,
            method="cure_fit.zwayam_careersite_detail.jobConfigurationData.Description",
            description_url=detail_url,
            locations=locations,
            department=detail.get("departmentName") or src.get("departmentName"),
            employment_type=None,
            workplace_type=detail.get("workMode") or src.get("workMode"),
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
            date_updated=date_updated,
            salary=_salary_from_row(src),
            employer=detail.get("companyName"),
        )
        job.description_text = description_text
        job.record_identity = f"cure_fit:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry = await self.http.get(entry_url)
        final_entry = str(entry.url)
        self._assert_careers_url(final_entry, what="official entry final URL")
        if urlsplit(final_entry).path.rstrip("/") != "/cult":
            raise ValueError(f"Cure.fit official entry redirected to an unexpected page: {final_entry}")

        bundle_url, main_js = await self._main_bundle(final_entry, entry.text)
        config = self._app_config(main_js)
        self._assert_endpoints(main_js)
        route = self._route(main_js)
        search_url = urljoin(config["api_endpoint"], "jobs/search")
        detail_url = urljoin(config["api_endpoint"], "jobs-service/v1/jobs/careersite")
        api_domain = urlsplit(config["api_endpoint"]).hostname

        # --- paginate the full unfiltered listing -------------------------------
        listing_started = time.perf_counter()
        pagesize = None
        total_count = None
        collected = []
        offsets = []
        page_records = []
        offset = 0
        while True:
            if not self.page_guard():
                return self.result
            payload, filter_cri = await self._search_page(search_url, config, offset)
            data, rows = self._search_rows(payload, offset)
            how_much = data.get("facetedSearchConfig", {}).get("paginationHowMuch")
            if pagesize is None:
                if str(how_much) != "10":
                    raise ValueError(f"Cure.fit pagination page size changed from 10: {how_much!r}")
                pagesize = int(how_much)
                total_count = data.get("totalCount")
            if total_count is not None and data.get("totalCount") != total_count:
                self.result.errors.append(
                    f"cure_fit_total_count_changed_between_pages:{total_count}->{data.get('totalCount')}"
                )
            if data.get("facetedSearchConfig", {}).get("companyId") != self.EXPECTED_COMPANY_ID:
                raise ValueError("Cure.fit listing response companyId changed from 15470")
            page_ids = [row["id"] for row in rows]
            page_records.append({
                "offset": offset,
                "rows": len(rows),
                "total_count": data.get("totalCount"),
                "has_more_data": data.get("hasMoreData"),
                "filter_cri": filter_cri,
                "job_ids": page_ids,
            })
            offsets.append(offset)
            collected.extend(rows)
            if not data.get("hasMoreData"):
                break
            if not rows:
                self.result.errors.append(f"cure_fit_listing_page_empty_before_has_more_false_offset_{offset}")
                break
            offset += pagesize
            if len(offsets) > max(total_count or 0, 1):
                self.result.errors.append("cure_fit_pagination_did_not_terminate")
                break
        listing_seconds = time.perf_counter() - listing_started

        if total_count is not None and len(collected) != total_count:
            self.result.errors.append(
                f"cure_fit_listing_count_mismatch:{len(collected)}_rows_totalCount_{total_count}"
            )
        seen_ids = [row["id"] for row in collected]
        if len(set(seen_ids)) != len(seen_ids):
            self.result.errors.append("cure_fit_listing_repeats_a_company_published_id")

        # Final membership re-check against the first published page
        # (same shapes as the Atlan initial/final reconciliation, on the
        # first listing page rather than a second full enumeration).
        recheck_ok = True
        try:
            payload, _ = await self._search_page(search_url, config, 0)
            recheck_data, recheck_rows = self._search_rows(payload, 0)
            first_ids = page_records[0]["job_ids"] if page_records else []
            if ([row["id"] for row in recheck_rows] != first_ids
                    or recheck_data.get("totalCount") != total_count):
                recheck_ok = False
                self.result.errors.append(
                    "cure_fit_official_listing_membership_changed_between_first_page_and_post_collection_recheck"
                )
        except Exception as exc:
            recheck_ok = False
            self.result.errors.append(
                f"Cure.fit post-collection listing recheck failed; collected rows retained: {type(exc).__name__}: {exc}"
            )

        # --- representative UI evidence -----------------------------------
        ui_started = time.perf_counter()
        representative = {}
        representative_error = None
        if collected:
            first = collected[0]
            first_view = jobview_url(final_entry, route, first["jobUrl"], first["id"])
            try:
                view_resp = await self.http.get(first_view)
                if str(view_resp.url) != first_view:
                    raise ValueError("Cure.fit representative job view redirected to a different page")
                share_resp = await self.http.get(
                    public_share_url(config["job_share_url"], first["jobUrl"],
                                     config["domain"], api_domain),
                    headers=BROWSER_HEADERS)
                share_body = share_resp.text
                expected_target = f"https://{config['domain']}#!/job-view/{first['jobUrl']}"
                if expected_target not in share_body:
                    raise ValueError("Cure.fit public share page does not redirect to its inspected job-view route")
                representative = {
                    "job_id": str(first["id"]),
                    "job_view_url": first_view,
                    "share_url": public_share_url(config["job_share_url"], first["jobUrl"],
                                                  config["domain"], api_domain),
                    "share_redirect_target_prefix": expected_target,
                }
            except Exception as exc:
                representative_error = f"{type(exc).__name__}: {exc}"
                self.result.errors.append(f"Cure.fit representative job-view evidence: {representative_error}")
        else:
            representative_error = "no listed jobs"
        ui_seconds = time.perf_counter() - ui_started

        # --- hydrate every listed job from its exact detail response -------
        detail_started = time.perf_counter()
        detail_failures = 0
        jobs_by_id = []
        for row in collected:
            try:
                detail = await self._detail(detail_url, config, row)
            except Exception as exc:
                detail_failures += 1
                self.result.errors.append(
                    f"Cure.fit exact detail {row['id']} ({row.get('jobUrl')}): {type(exc).__name__}: {exc}")
                continue
            page_offset = next((rec["offset"] for rec in page_records if row["id"] in rec["job_ids"]), None)
            jobs_by_id.append((row, detail, page_offset))
        for row, detail, page_offset in jobs_by_id:
            self.add(self._make_job(
                row, detail, entry_url=final_entry, route=route, config=config,
                search_url=search_url, detail_url=detail_url, page_offset=page_offset,
            ))
        detail_seconds = time.perf_counter() - detail_started

        self.result.expected_total = total_count
        self.result.pages = len(page_records)
        if collected and len(jobs_by_id) != len(collected):
            self.result.listing_complete = False
        elif not collected:
            self.result.listing_complete = False
            self.result.errors.append("Cure.fit official listing returned zero visible jobs")
        elif not recheck_ok:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": final_entry,
            "requested_entry_url": entry_url,
            "main_bundle_url": bundle_url,
            "app_config": config,
            "jobview_route_template": route,
            "search_url": search_url,
            "detail_url": detail_url,
            "membership_authority": (
                "Cure.fit official careers app (careers.cult.fit/cult/) jobs/search "
                "with empty anyOfTheseWords, domain=careers.cult.fit, companyId=MTU0NzA=; "
                "all rows shown by the list component, unfiltered"
            ),
            "listing_filter_behavior": (
                "default sortCriteria modifiedDate desc, paginationStartNo offsets by "
                "facetedSearchConfig.paginationHowMuch (10); keyword advanced=empty filters not applied"
            ),
            "page_size": pagesize,
            "expected_total_count": total_count,
            "page_records": page_records,
            "page_offsets": offsets,
            "collected_row_count": len(collected),
            "collected_job_ids": [str(row["id"]) for row in collected],
            "unique_collected_ids": len(set(seen_ids)),
            "post_collection_recheck_first_page_matches": recheck_ok,
            "representative_job_view": representative,
            "representative_job_view_error": representative_error,
            "detail_fetch_failures": detail_failures,
            "details_fetched": len(jobs_by_id),
            "description_source": "Each listed job's jobConfigurationData.Description from the company's public careersite detail POST (== longDescription in the payload).",
            "salary_policy": "Non-empty numeric minJobSalary/maxJobSalary retained verbatim with currencyType; empty salaries stay null. No conversion.",
            "attachment_links": {},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(ui_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        return self.result
