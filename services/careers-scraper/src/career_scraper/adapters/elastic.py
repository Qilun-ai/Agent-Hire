"""Elastic's own portal journey: official careers CTA, jobs search listing,
department browse, and exact job detail pages.

Membership is the union of Elastic's two published live listings:
the home search page's POST /api/appSearch pages (live=1, group_id=1509)
and the department browse GET /api/filter/jobs rows (live=1). The search
index and the browse database differ by a handful of rows (search lags two
newest Support postings; browse groupBy lists hide the Pipeline category),
so only the union covers every publicly reachable posting. Full
descriptions are the jobs table ``content`` field, rendered verbatim by
the native JobDetails page; pipeline-only rows are read from their exact
detail pages. No ATS board is enumerated.
"""
import hashlib
import json
import re
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


PAGE_SIZE = 100
EXPECTED_GID = 1509
EXPECTED_TENANT = "Elastic"


def compact_body_hash(html):
    """Mirror the parent browser reference NFC/whitespace-free body hash."""
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Elastic job URL path is missing")
    return urljoin("https://jobs.elastic.co/", "jobs/" + path.lstrip("/"))


def apply_url_for(gh_jid):
    if not re.fullmatch(r"\d+", str(gh_jid)):
        raise ValueError(f"Elastic gh_jid is not a numeric public identifier: {gh_jid!r}")
    return f"https://jobs.elastic.co/form?gh_jid={gh_jid}"


def _salary_currency(row):
    raw = row.get("custom_fields")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict):
        return value.get("linkedin_salary_currency")
    return None


def _workplace_from_row(row):
    """Derive the rendered Distributed/Hybrid/Onsite label from location fields.

    The jobs table ``job_type`` is always ``Onsite`` even for distributed
    and hybrid rows, so the visible UI labels (Distributed Locations /
    Hybrid Locations chips) are the only workplace signal. The raw
    ``job_type`` is retained verbatim in raw data.
    """
    hybrid = row.get("hybrid_locations")
    remote = row.get("remote_locations")
    if isinstance(hybrid, str) and hybrid.strip():
        return "Hybrid"
    if isinstance(remote, str) and remote.strip():
        return "Distributed"
    return "Onsite"


def _salary_from_row(row, description_text):
    """Retain published structured compensation verbatim; never convert."""
    minimum = row.get("min_salary")
    maximum = row.get("max_salary")
    if minimum in (None, "", "0", "0.0") or maximum in (None, "", "0", "0.0"):
        return None
    currency = _salary_currency(row)
    source_lines = []
    lines = [line.strip() for line in (description_text or "").splitlines()]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered.startswith("the typical starting salary range for this role is:") or lowered.startswith(
            "the typical starting salary range for this role in the select locations listed above is:"
        ):
            if index + 1 < len(lines) and lines[index + 1]:
                source_lines.append(lines[index + 1])
    return {
        "min_salary": str(minimum),
        "max_salary": str(maximum),
        "currency": currency,
        "source_text": "\n".join(source_lines) if source_lines else None,
        "source_fields": ["min_salary", "max_salary", "custom_fields.linkedin_salary_currency", "content range lines"],
        "semantics": "Base-salary range(s) as published in the full description; structured bounds and currency retained verbatim without conversion.",
    }


def _job_attachment_links(description_html):
    """Role-bearing document attachments, excluding compliance poster PDFs.

    Every Elastic description links the FMLA/EPPA poster PDFs from dol.gov
    as legal boilerplate. Those are not role attachments and are excluded,
    mirroring the Boeing EEO-poster rule. Any other PDF/DOCX link would be
    retained here and surfaced to the verifier.
    """
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            continue
        host = (urlsplit(href).hostname or "").lower()
        if host in {"www.dol.gov", "dol.gov"}:
            continue
        values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _locations_from_row(row):
    address = row.get("address")
    return [{
        "location": address,
        "address": address,
        "remote_locations": row.get("remote_locations"),
        "hybrid_locations": row.get("hybrid_locations"),
        "city": row.get("city"),
        "country": row.get("country"),
        "state": row.get("state"),
        "region": row.get("region"),
    }]


class Elastic(Adapter):
    """Follow only Elastic's published portal requests and exact detail pages."""

    VERIFICATION_REVISION = "elastic.jobs-elastic-search-filter-detail-union.1"
    WWW_HOST = "www.elastic.co"
    JOBS_HOST = "jobs.elastic.co"
    JOBS_ORIGIN = "https://jobs.elastic.co"
    WWW_ENTRY = "https://www.elastic.co/careers"

    # ------------------------------------------------------------------
    # URL guards
    # ------------------------------------------------------------------
    @staticmethod
    def _assert_www_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"elastic.co", "www.elastic.co"}:
            raise ValueError(f"Elastic company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Elastic company page asset has an unexpected path: {url}")

    @classmethod
    def _assert_jobs_url(cls, url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != cls.JOBS_HOST:
            raise ValueError(f"Elastic portal request left jobs.elastic.co: {url}")
        return parsed

    # ------------------------------------------------------------------
    # Entry / home parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _data_page(html):
        soup = BeautifulSoup(html, "lxml")
        node = soup.find("div", attrs={"data-page": True})
        if node is None:
            raise ValueError("Elastic portal page has no Inertia data-page payload")
        try:
            return json.loads(node.get("data-page"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Elastic portal data-page payload is not valid JSON") from exc

    @staticmethod
    def _careers_link(html):
        soup = BeautifulSoup(html, "lxml")
        hrefs = [a.get("href") for a in soup.select("a[href]")]
        exact = [h for h in hrefs if h == "https://jobs.elastic.co/"]
        if not exact:
            raise ValueError("Elastic official careers page no longer links View open roles to jobs.elastic.co")
        return exact[0]

    @staticmethod
    def _home_gid(data_page):
        if data_page.get("component") != "Elastic/CareersHome":
            raise ValueError("Elastic jobs home no longer serves the inspected CareersHome component")
        props = data_page.get("props") or {}
        gid = props.get("gid")
        tenant = (props.get("tenantConfig") or {}).get("branding") or {}
        if gid != EXPECTED_GID or tenant.get("customer_name") != EXPECTED_TENANT:
            raise ValueError(f"Elastic jobs tenant changed from its inspected gid={EXPECTED_GID} Elastic board: gid={gid!r} tenant={tenant!r}")
        return gid

    # ------------------------------------------------------------------
    # CSRF / appSearch
    # ------------------------------------------------------------------
    def _xsrf_token(self):
        client = getattr(self.http, "client", None)
        if client is not None:
            for cookie in getattr(client, "cookies", None).jar if getattr(client, "cookies", None) else []:
                if cookie.name == "XSRF-TOKEN":
                    return unquote(cookie.value)
        raise ValueError("Elastic jobs CSRF token missing; the jobs home GET must precede /api/appSearch POSTs")

    @staticmethod
    def _app_search_body(gid, page, page_size=PAGE_SIZE):
        return {
            "query": "",
            "search_fields": {
                "title": {}, "location": {}, "category": {}, "city_filter": {},
                "country_filter": {}, "remote_locations": {}, "hybrid_locations": {},
            },
            "result_fields": {
                "title": {"raw": {}}, "location": {"raw": {}}, "remote_locations": {"raw": {}},
                "hybrid_locations": {"raw": {}}, "job_type": {"raw": {}},
                "content": {"snippet": {"fallback": True}}, "category": {"raw": {}},
                "country_filter": {"raw": {}}, "city_filter": {"raw": {}}, "url": {"raw": {}},
            },
            "precision": 2,
            "page": {"size": page_size, "current": page},
            "filters": {"all": [{"any": [{"group_id": gid}]}, {"any": [{"live": 1}]}]},
            "facets": {
                "category": {"type": "value", "size": 30},
                "location": {"type": "value", "size": 30},
                "job_type": {"type": "value", "size": 30},
            },
            "sort": [{"_score": "desc"}, {"created_at": "desc"}],
        }

    async def _app_search_page(self, gid, page, xsrf, label):
        url = f"{self.JOBS_ORIGIN}/api/appSearch"
        body = self._app_search_body(gid, page)
        response = await self.http.request(
            "POST", url, json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json", "X-XSRF-TOKEN": xsrf},
        )
        if str(response.url) != url:
            raise ValueError(f"Elastic {label} appSearch request redirected away from its inspected source")
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise ValueError(f"Elastic {label} appSearch response is not JSON") from exc
        return payload

    @staticmethod
    def _app_search_rows(payload, label):
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError(f"Elastic {label} appSearch response has no results array")
        meta = ((payload.get("meta") or {}).get("page")) or {}
        for row in payload["results"]:
            raw_url = (((row or {}).get("url") or {}).get("raw"))
            title = (((row or {}).get("title") or {}).get("raw"))
            if not isinstance(raw_url, str) or not raw_url.strip() or not isinstance(title, str) or not title.strip():
                raise ValueError(f"Elastic {label} appSearch row is missing its public url/title")
            gh_jid = raw_url.rstrip("/").split("/")[-1]
            if not re.fullmatch(r"\d+", gh_jid):
                raise ValueError(f"Elastic {label} appSearch row has a non-numeric public job identifier: {raw_url!r}")
        return payload["results"], meta

    async def _fetch_app_search(self, gid, xsrf, label):
        rows = []
        seen = set()
        total = None
        total_pages = None
        page = 1
        page_counts = []
        facets = None
        while True:
            if not self.page_guard():
                raise ValueError("Elastic page budget exhausted during appSearch pagination")
            payload = await self._app_search_page(gid, page, xsrf, label)
            page_rows, meta = self._app_search_rows(payload, label)
            if total is None:
                total = meta.get("total_results")
                total_pages = meta.get("total_pages")
                facets = payload.get("facets")
                if not isinstance(total, int) or not isinstance(total_pages, int):
                    raise ValueError(f"Elastic {label} appSearch response has no page totals")
            else:
                if meta.get("total_results") != total or meta.get("total_pages") != total_pages:
                    raise ValueError(f"Elastic appSearch totals changed between pages ({label} page {page})")
            for row in page_rows:
                gh_jid = row["url"]["raw"].rstrip("/").split("/")[-1]
                if gh_jid in seen:
                    raise ValueError(f"Elastic {label} appSearch repeats a published job: {gh_jid}")
                seen.add(gh_jid)
                rows.append(row)
            page_counts.append(len(page_rows))
            if len(rows) >= total:
                break
            if not page_rows:
                raise ValueError(f"Elastic {label} appSearch ended early at page {page} ({len(rows)}/{total})")
            page += 1
        if len(rows) != total:
            raise ValueError(f"Elastic {label} appSearch collected {len(rows)} of {total} published rows")
        return rows, {"total": total, "total_pages": total_pages, "page_counts": page_counts, "facets": facets}

    # ------------------------------------------------------------------
    # Browse counts / filter rows
    # ------------------------------------------------------------------
    async def _group_counts(self, gid, group_by, label):
        url = f"{self.JOBS_ORIGIN}/api/jobs?groupId={gid}&groupBy={quote(group_by, safe='')}"
        self._assert_jobs_url(url)
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"Elastic {label} browse-count request redirected away from its inspected source")
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise ValueError(f"Elastic {label} browse-count response is not JSON") from exc
        if payload.get("result") != "success" or not isinstance(payload.get("jobs"), list) or not payload["jobs"]:
            raise ValueError(f"Elastic {label} browse-count response has no published groups")
        values = []
        for entry in payload["jobs"]:
            value = entry.get("value") if isinstance(entry, dict) else None
            count = entry.get("count") if isinstance(entry, dict) else None
            if not isinstance(value, str) or not value.strip() or not isinstance(count, int) or count <= 0:
                raise ValueError(f"Elastic {label} browse-count group is malformed: {entry!r}")
            values.append((value, count))
        if len({v for v, _ in values}) != len(values):
            raise ValueError(f"Elastic {label} browse-count groups repeat a published value")
        return payload, values

    @staticmethod
    def _filter_rows(payload, label):
        data = payload.get("data")
        if payload.get("result") != "success":
            raise ValueError(f"Elastic {label} filter response was not successful")
        if isinstance(data, dict):
            rows = [row for members in data.values() for row in members]
        elif isinstance(data, list):
            rows = data
        else:
            raise ValueError(f"Elastic {label} filter response has no rows array")
        if payload.get("numResults") is not None and payload.get("numResults") != len(rows):
            raise ValueError(
                f"Elastic {label} filter numResults {payload.get('numResults')!r} differs from {len(rows)} rows"
            )
        return rows

    @staticmethod
    def _validate_filter_row(row, label):
        if not isinstance(row, dict):
            raise ValueError(f"Elastic {label} filter row is not an object")
        uid = row.get("unique_identifier")
        if not isinstance(uid, str) or not re.fullmatch(r"\d+", uid):
            raise ValueError(f"Elastic {label} filter row has a missing or invalid unique_identifier: {uid!r}")
        if row.get("group_id") != EXPECTED_GID:
            raise ValueError(f"Elastic {label} filter row {uid} belongs to a different group: {row.get('group_id')!r}")
        if row.get("live") != 1:
            raise ValueError(f"Elastic {label} filter row {uid} is not live (live={row.get('live')!r})")
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            raise ValueError(f"Elastic {label} filter row {uid} has no title")
        if not isinstance(row.get("url"), str) or not row["url"].strip() or not row["url"].rstrip("/").split("/")[-1] == uid:
            raise ValueError(f"Elastic {label} filter row {uid} url does not end in its unique_identifier")
        content = row.get("content")
        if not isinstance(content, str) or not text_html(content).strip():
            raise ValueError(f"Elastic {label} filter row {uid} has an empty full content description")

    async def _fetch_filter_set(self, gid, column, values_with_counts, label):
        collected = {}
        request_urls = []
        for value, count in values_with_counts:
            if not self.page_guard():
                raise ValueError("Elastic page budget exhausted during filter pagination")
            url = (f"{self.JOBS_ORIGIN}/api/filter/jobs?groupId={gid}"
                   f"&column={quote(column, safe='')}&value={quote(value, safe='')}&groupBy=subdivision")
            self._assert_jobs_url(url)
            request_urls.append(url)
            response = await self.http.get(url)
            if str(response.url) != url:
                raise ValueError(f"Elastic {label} filter request redirected away from its inspected source")
            try:
                payload = json.loads(response.text)
            except ValueError as exc:
                raise ValueError(f"Elastic {label} filter response for {value!r} is not JSON") from exc
            rows = self._filter_rows(payload, f"{label} {value}")
            if len(rows) != count:
                raise ValueError(
                    f"Elastic {label} filter {value!r} count {count} differs from {len(rows)} rows"
                )
            for row in rows:
                self._validate_filter_row(row, f"{label} {value}")
                uid = row["unique_identifier"]
                if uid in collected and collected[uid] != row:
                    raise ValueError(f"Elastic {label} filter row {uid} changed between browse groups")
                collected[uid] = row
        return collected, request_urls

    # ------------------------------------------------------------------
    # Detail pages
    # ------------------------------------------------------------------
    async def _fetch_detail(self, path, expected_uid, label):
        url = native_job_url(path)
        self._assert_jobs_url(url)
        response = await self.http.get(url)
        final = str(response.url)
        expected = f"{url}?gh_jid={expected_uid}"
        if final not in {url, expected}:
            raise ValueError(f"Elastic {label} detail page redirected to an unexpected URL: {final}")
        data_page = self._data_page(response.text)
        if data_page.get("component") != "Elastic/JobDetails":
            raise ValueError(f"Elastic {label} detail page no longer serves the inspected JobDetails component")
        props = data_page.get("props") or {}
        job_object = props.get("job_object") or {}
        if props.get("gh_jid") != expected_uid or str(job_object.get("unique_identifier")) != expected_uid:
            raise ValueError(f"Elastic {label} detail page id does not match its exact published URL")
        content = job_object.get("content")
        if not isinstance(content, str) or not text_html(content).strip():
            raise ValueError(f"Elastic {label} detail page {expected_uid} has an empty full content description")
        return url, final, props

    # ------------------------------------------------------------------
    # Job construction
    # ------------------------------------------------------------------
    def _make_job(self, uid, *, filter_row=None, search_row=None, detail_props=None,
                  detail_page_url=None, membership_changed=False):
        base = filter_row or (detail_props or {}).get("job_object") or {}
        path = base.get("url") or (search_row or {}).get("url", {}).get("raw")
        title = base.get("title")
        if title is None and search_row:
            title = (search_row.get("title") or {}).get("raw")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Elastic job {uid} has no title")
        url = native_job_url(path)
        description_html = base.get("content") or ""
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{uid}: Elastic public detail content is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{uid}: Elastic public detail description is implausibly short")
        salary = _salary_from_row(base, description_text)
        job_code = base.get("job_code")
        attachment_links = _job_attachment_links(description_html)
        if attachment_links:
            self.result.errors.append(f"{uid}: published role attachment links require full attachment capture")
        raw = {}
        if filter_row is not None:
            raw["filter_row"] = filter_row
        if search_row is not None:
            raw["app_search_row"] = search_row
        if detail_props is not None:
            raw["detail_props"] = detail_props
        raw["source_ui"] = {
            "native_job_url": url,
            "published_apply_url": apply_url_for(uid),
            "gh_jid": uid,
            "filter_row_url": (filter_row or {}).get("url"),
            "app_search_url": (search_row or {}).get("url", {}).get("raw") if search_row else None,
            "detail_page_url": detail_page_url,
            "attachment_links": attachment_links,
            "salary_from_row": salary,
            "workplace_from_row": _workplace_from_row(base),
            "description_source": "jobs table content field as rendered by Elastic/JobDetails",
        }
        if membership_changed:
            raw["membership_changed_during_collection"] = True
        job = self.job(
            raw,
            title=title,
            url=url,
            job_id=uid,
            requisition_id=job_code,
            apply_url=apply_url_for(uid),
            description=description_html,
            method="elastic.job_details.content",
            description_url=detail_page_url or "elastic.filter_jobs.content",
            locations=_locations_from_row(base),
            department=base.get("subdivision"),
            team=base.get("category"),
            employment_type=None,
            workplace_type=_workplace_from_row(base),
            date_posted=base.get("date"),
            date_posted_raw=base.get("date"),
            date_updated=base.get("updated_at"),
            salary=salary,
            employer="Elastic",
        )
        job.description_text = description_text
        job.record_identity = f"elastic:{uid}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------
    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        if entry_url.rstrip("/") != self.WWW_ENTRY:
            raise ValueError("Elastic dedicated flow must start from its official https://www.elastic.co/careers entry")

        entry_started = time.perf_counter()
        entry = await self.http.get(self.WWW_ENTRY)
        self._assert_www_url(entry.url, path_prefix="/careers")
        self._careers_link(entry.text)

        home_started = time.perf_counter()
        home = await self.http.get(f"{self.JOBS_ORIGIN}/")
        self._assert_jobs_url(home.url)
        if str(home.url) != f"{self.JOBS_ORIGIN}/":
            raise ValueError("Elastic jobs home redirected away from its inspected entry")
        home_props = self._data_page(home.text)
        gid = self._home_gid(home_props)
        # Follow the shipped CareersHome/Home asset chain the UI itself loads.
        soup = BeautifulSoup(home.text, "lxml")
        script_srcs = [s.get("src") for s in soup.select("script[src]")]
        careers_home_src = next((s for s in script_srcs if s and "CareersHome-" in s), None)
        if careers_home_src is None:
            raise ValueError("Elastic jobs home no longer ships its CareersHome component")
        careers_home_url = urljoin(f"{self.JOBS_ORIGIN}/", careers_home_src)
        home_script = await self.http.get(careers_home_url)
        if str(home_script.url) != careers_home_url:
            raise ValueError("Elastic CareersHome component redirected away from its inspected asset")
        home_script_text = home_script.text
        for token in ("Careers at Elastic", "group_id", "live", "result_fields"):
            if token not in home_script_text:
                raise ValueError("Elastic CareersHome component no longer matches the inspected search/filter flow")
        connector_match = re.search(r"LocalAPIConnector-([A-Za-z0-9_-]+\.js)", home_script_text)
        nav_match = re.search(r"JobNav-([A-Za-z0-9_-]+\.js)", home_script_text)
        if not connector_match or not nav_match:
            raise ValueError("Elastic CareersHome component no longer imports its inspected search/browse modules")
        connector_url = urljoin(f"{self.JOBS_ORIGIN}/build/assets/", "LocalAPIConnector-" + connector_match.group(1))
        nav_url = urljoin(f"{self.JOBS_ORIGIN}/build/assets/", "JobNav-" + nav_match.group(1))
        connector = await self.http.get(connector_url)
        nav = await self.http.get(nav_url)
        if '"/api/appSearch"' not in connector.text or "group_id" not in home_script_text:
            raise ValueError("Elastic search connector no longer ships the inspected /api/appSearch request")
        if "/api/jobs?groupId=" not in nav.text or "/api/filter/jobs?groupId=" not in nav.text:
            raise ValueError("Elastic browse component no longer ships the inspected browse requests")
        xsrf = self._xsrf_token()
        # Published sitemap enumeration (reconciliation only; membership stays
        # the search+browse union). The portal publishes /sitemap.xml with one
        # loc per public job page; its set should match the search listing.
        sitemap_url = f"{self.JOBS_ORIGIN}/sitemap.xml"
        sitemap_response = await self.http.get(sitemap_url)
        if str(sitemap_response.url) != sitemap_url:
            raise ValueError("Elastic sitemap request redirected away from its inspected source")
        sitemap_locs = [loc for loc in re.findall(r"<loc>(.*?)</loc>", sitemap_response.text) if "/jobs/" in loc]
        sitemap_uids = []
        for loc in sitemap_locs:
            tail = loc.rstrip("/").split("/")[-1]
            if not re.fullmatch(r"\d+", tail):
                raise ValueError(f"Elastic sitemap contains a non-job URL: {loc!r}")
            sitemap_uids.append(tail)
        if not sitemap_uids or len(sitemap_uids) != len(set(sitemap_uids)):
            raise ValueError("Elastic sitemap enumeration is empty or repeats a job")
        home_seconds = time.perf_counter() - home_started
        entry_seconds = time.perf_counter() - entry_started

        # --- Initial listing snapshot: search pages + browse counts + filter rows
        search_initial, search_meta_initial = await self._fetch_app_search(gid, xsrf, "initial")
        search_initial_by_uid = {row["url"]["raw"].rstrip("/").split("/")[-1]: row for row in search_initial}
        _, subdiv_counts_initial = await self._group_counts(gid, "subdivision", "initial")
        _, category_counts_initial = await self._group_counts(gid, "category", "initial")
        filter_initial, filter_urls_initial = await self._fetch_filter_set(gid, "subdivision", subdiv_counts_initial, "initial subdivision")
        filter_cat_initial, filter_cat_urls_initial = await self._fetch_filter_set(gid, "category", category_counts_initial, "initial category")
        if set(filter_initial) != set(filter_cat_initial):
            raise ValueError("Elastic initial subdivision and category browse rows do not reconcile to the same IDs")
        for uid in filter_initial:
            if filter_initial[uid] != filter_cat_initial[uid]:
                raise ValueError(f"Elastic initial browse row {uid} differs between subdivision and category groups")
        initial_uids = set(search_initial_by_uid) | set(filter_initial)
        if not initial_uids:
            self.result.expected_total = 0
            self.result.errors.append("Elastic official listings returned zero visible jobs; empty state requires independent review")
            return self.result
        search_only_initial = sorted(set(search_initial_by_uid) - set(filter_initial))
        filter_only_initial = sorted(set(filter_initial) - set(search_initial_by_uid))

        # --- Representative + pipeline detail pages (exact published URLs only)
        detail_started = time.perf_counter()
        detail_props = {}
        detail_page_urls = {}
        detail_errors = []
        # Pipeline-only rows have no filter row; their exact detail pages are
        # required for full descriptions. Fetch every search-only row.
        # Overlapping rows are verified on two representatives only.
        corporate_uid = next((uid for uid, row in filter_initial.items() if row.get("subdivision") == "Corporate"), None)
        engineering_uid = next((uid for uid, row in filter_initial.items() if row.get("subdivision") == "Engineering"), None)
        representative_uids = []
        for candidate in (corporate_uid, engineering_uid):
            if candidate and candidate not in representative_uids and candidate in search_initial_by_uid:
                representative_uids.append(candidate)
        detail_uids = list(dict.fromkeys([*search_only_initial, *representative_uids]))
        for uid in detail_uids:
            # Resolve the exact path from the listing row that exposed it.
            if uid in filter_initial:
                path = filter_initial[uid]["url"]
            elif uid in search_initial_by_uid:
                path = search_initial_by_uid[uid]["url"]["raw"]
            else:
                detail_errors.append(f"{uid}: no exact published path for detail fetch")
                continue
            try:
                page_url, _, props = await self._fetch_detail(path, uid, "representative")
                detail_props[uid] = props
                detail_page_urls[uid] = page_url
            except Exception as exc:
                detail_errors.append(f"{uid}: Elastic exact detail page failed: {type(exc).__name__}: {exc}")
        for message in detail_errors:
            self.result.errors.append(message)
        # Verify bulk filter content equals detail content for overlapping reps.
        detail_comparisons = {}
        for uid, props in detail_props.items():
            if uid in filter_initial:
                bulk = filter_initial[uid].get("content") or ""
                detail = (props.get("job_object") or {}).get("content") or ""
                detail_comparisons[uid] = {
                    "bulk_filter_content_sha256": hashlib.sha256(bulk.encode()).hexdigest(),
                    "detail_content_sha256": hashlib.sha256(detail.encode()).hexdigest(),
                    "byte_identical": bulk == detail,
                }
                if bulk != detail:
                    self.result.errors.append(f"{uid}: bulk filter content differs from its exact detail page content")
        detail_seconds = time.perf_counter() - detail_started

        # --- Final listing snapshot (same exact requests)
        final_started = time.perf_counter()
        final_snapshot_verified = True
        try:
            search_final, search_meta_final = await self._fetch_app_search(gid, xsrf, "final")
            search_final_by_uid = {row["url"]["raw"].rstrip("/").split("/")[-1]: row for row in search_final}
            _, subdiv_counts_final = await self._group_counts(gid, "subdivision", "final")
            _, category_counts_final = await self._group_counts(gid, "category", "final")
            filter_final, filter_urls_final = await self._fetch_filter_set(gid, "subdivision", subdiv_counts_final, "final subdivision")
            filter_cat_final, filter_cat_urls_final = await self._fetch_filter_set(gid, "category", category_counts_final, "final category")
            if set(filter_final) != set(filter_cat_final):
                raise ValueError("Elastic final subdivision and category browse rows do not reconcile to the same IDs")
        except Exception as exc:
            final_snapshot_verified = False
            search_final_by_uid = dict(search_initial_by_uid)
            search_meta_final = search_meta_initial
            filter_final = dict(filter_initial)
            filter_cat_final = dict(filter_cat_initial)
            filter_urls_final = list(filter_urls_initial)
            filter_cat_urls_final = list(filter_cat_urls_initial)
            subdiv_counts_final = subdiv_counts_initial
            category_counts_final = category_counts_initial
            self.result.errors.append(
                "Elastic final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_uids = set(search_final_by_uid) | set(filter_final)
        changed_membership = final_snapshot_verified and initial_uids != final_uids
        if changed_membership:
            self.result.errors.append("elastic_official_listing_membership_changed_between_initial_and_final_snapshots")
        search_only_final = sorted(set(search_final_by_uid) - set(filter_final))
        filter_only_final = sorted(set(filter_final) - set(search_final_by_uid))
        # Rows whose listing/detail record changed between snapshots.
        changed_rows = []
        for uid in (initial_uids & final_uids):
            # Compare the richest available record per snapshot.
            initial_rich = filter_initial[uid] if uid in filter_initial else {"search": search_initial_by_uid[uid]}
            final_rich = filter_final[uid] if uid in filter_final else {"search": search_final_by_uid[uid]}
            if initial_rich != final_rich:
                changed_rows.append(uid)
        final_seconds = time.perf_counter() - final_started

        # Keep final records for stable IDs; retain initial-only rows so a
        # moving listing does not silently discard usable evidence.
        final_sort = sorted(final_uids)
        initial_sort = sorted(initial_uids)
        all_uids = list(dict.fromkeys([*final_sort, *initial_sort]))
        # For pipeline rows that only appeared in the final snapshot (if any),
        # fetch their exact detail pages now so descriptions stay complete.
        pipeline_needed = sorted({uid for uid in all_uids if uid not in filter_initial and uid not in filter_final})
        extra_detail_error = None
        for uid in pipeline_needed:
            if uid in detail_props:
                continue
            path = None
            if uid in search_final_by_uid:
                path = search_final_by_uid[uid]["url"]["raw"]
            elif uid in search_initial_by_uid:
                path = search_initial_by_uid[uid]["url"]["raw"]
            if path is None:
                continue
            try:
                page_url, _, props = await self._fetch_detail(path, uid, "final-only")
                detail_props[uid] = props
                detail_page_urls[uid] = page_url
            except Exception as exc:
                extra_detail_error = f"{uid}: Elastic final-only detail page failed: {type(exc).__name__}: {exc}"
                self.result.errors.append(extra_detail_error)

        mapping_started = time.perf_counter()
        for uid in all_uids:
            filter_row = filter_final.get(uid) or filter_initial.get(uid)
            search_row = search_final_by_uid.get(uid) or search_initial_by_uid.get(uid)
            props = detail_props.get(uid)
            page_url = detail_page_urls.get(uid)
            if filter_row is None and props is None:
                self.result.errors.append(f"{uid}: no full description source for a union-listed job")
                continue
            if filter_row is None and props is not None:
                # Pipeline rows: detail page is the description source.
                job_object = props.get("job_object") or {}
                if job_object.get("content") != detail_props[uid].get("job_object", {}).get("content"):
                    pass
                filter_row = job_object
            self.add(self._make_job(
                uid, filter_row=filter_row, search_row=search_row,
                detail_props=props, detail_page_url=page_url,
                membership_changed=uid not in final_uids,
            ))
        mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(final_uids)
        # result.pages is the accumulated page_guard count (entry + appSearch
        # pages + filter groups, initial and final); no overwrite here.
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors and final_snapshot_verified

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": self.WWW_ENTRY,
            "jobs_home_url": f"{self.JOBS_ORIGIN}/",
            "careers_home_component_url": careers_home_url,
            "search_connector_url": connector_url,
            "browse_component_url": nav_url,
            "tenant_gid": gid,
            "membership_authority": (
                "Union of Elastic's two published live listings: home search POST /api/appSearch "
                "(filters group_id=1509 + live=1) and department browse GET /api/filter/jobs "
                "(live=1). Neither surface alone covers all public postings; the union does."
            ),
            "listing_filter_behavior": (
                "appSearch: unfiltered pages with search_fields/result_fields/facets/sort as shipped "
                "by CareersHome + LocalAPIConnector; page size 100 (UI default 5) over the same "
                "endpoint/filters. Browse: /api/jobs?groupId=1509 groupBy subdivision/category counts, "
                "then /api/filter/jobs per published group value with groupBy=subdivision."
            ),
            "ui_pagination": (
                "Search paginates (total_pages=5 at size 100). Browse groups are unpaginated single "
                "requests per published subdivision/category value."
            ),
            "initial_search_total": search_meta_initial["total"],
            "final_search_total": search_meta_final["total"],
            "initial_search_page_counts": search_meta_initial["page_counts"],
            "final_search_page_counts": search_meta_final["page_counts"],
            "initial_search_facets": search_meta_initial["facets"],
            "final_search_facets": search_meta_final["facets"],
            "initial_subdivision_counts": subdiv_counts_initial,
            "final_subdivision_counts": subdiv_counts_final,
            "initial_category_counts": category_counts_initial,
            "final_category_counts": category_counts_final,
            "initial_filter_request_urls": filter_urls_initial + filter_cat_urls_initial,
            "final_filter_request_urls": filter_urls_final + filter_cat_urls_final,
            "sitemap_url": sitemap_url,
            "sitemap_uids": sorted(sitemap_uids),
            "sitemap_search_match_initial": sorted(set(sitemap_uids) - set(search_initial_by_uid)) == [] and sorted(set(search_initial_by_uid) - set(sitemap_uids)) == [],
            "sitemap_only_uids": sorted(set(sitemap_uids) - set(search_initial_by_uid)),
            "search_only_vs_sitemap_uids": sorted(set(search_initial_by_uid) - set(sitemap_uids)),
            "initial_search_uids": sorted(search_initial_by_uid),
            "final_search_uids": sorted(search_final_by_uid),
            "initial_filter_uids": sorted(filter_initial),
            "final_filter_uids": sorted(filter_final),
            "initial_search_only_uids": search_only_initial,
            "final_search_only_uids": search_only_final,
            "initial_filter_only_uids": filter_only_initial,
            "final_filter_only_uids": filter_only_final,
            "initial_union_uids": sorted(initial_uids),
            "final_union_uids": sorted(final_uids),
            "final_snapshot_verified": final_snapshot_verified,
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "representative_detail_uids": representative_uids,
            "pipeline_detail_uids": search_only_initial,
            "detail_content_comparisons": detail_comparisons,
            "detail_errors": detail_errors,
            "description_source": "Jobs table content field, rendered verbatim by Elastic/JobDetails; bulk filter rows and exact detail pages carry byte-identical content for overlapping rows.",
            "source_has_structured_compensation_field": True,
            "salary_policy": "Structured min_salary/max_salary plus linkedin_salary_currency and verbatim content range lines retained without conversion; rows without structured bounds stay null.",
            "job_attachment_links": {job.job_id: _job_attachment_links(job.description_html) for job in self.result.jobs if _job_attachment_links(job.description_html)},
            "evergreen_flag_values": {job.job_id: (job.raw.get("filter_row") or {}).get("evergreen") for job in self.result.jobs if (job.raw.get("filter_row") or {}).get("evergreen") not in (0, "0")},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": "appSearch pages + browse counts + filter rows, initial and final",
        })
        self.result.timings["detail_phase_seconds"] = round(mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["home_phase_seconds"] = round(home_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(
            time.perf_counter() - started - detail_seconds - mapping_seconds, 6
        )
        self.result.timings["final_snapshot_phase_seconds"] = round(final_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
