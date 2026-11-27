"""Baker Hughes's own Phenom search and native job-detail journey.

Membership authority is the unfiltered global search on the official
Baker Hughes careers site (``/widgets`` ``refineSearch``). Full
descriptions come from the site's own ``jobDetail`` widget request for
each exact listed ``jobSeqNo``; truncated teasers and AI/ML summaries
are never descriptions, and no Workday board is enumerated.
"""
import gzip
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import HTTP
from ..models import now
from .. import access
from ..structured import embedded_page_object

VERIFICATION_REVISION = "baker-hughes.native-phenom-widgets-refine-and-jobdetail.1"

# Observed in the official entry HTML fetched 2026-11-07T08:59Z
# (companies/baker-hughes/research/raw/official-search-results-20261107.html).
# Used only as an explicit fallback when the live entry HTML is
# Incapsula-challenged and the stealth escalation cannot settle it; the run
# stays partial in that case.
_ARCHIVED_ENTRY_CONFIG = {
    "widgetApiEndpoint": "https://careers.bakerhughes.com/widgets",
    "country": "global",
    "deviceType": "desktop",
    "locale": "en_global",
    "pageName": "search-results",
    "siteType": "external",
    "refNum": "BAHUGLOBAL",
    "baseUrl": "https://careers.bakerhughes.com/global/en/",
}

# Truncated previews, generated/ML embedding fields, hashes and unrelated
# personnel are omitted from retained evidence. Every other public field
# (including questionnaire IDs, CMS/standardised metadata and
# isEverGreenReq) is retained.
_DENY_KEYS = frozenset(
    {
        "descriptionTeaser",
        "ml_job_parser",
        "hiringManager",
        "ai_summary",
        "emb_complete_desc",
        "emb_hash",
        "fesHash",
        "hashKey",
        "TestingNew",
        "recruiterfirstName",
        "cmsJobChatbot",
    }
)

# Exact character class from ph-common-bundle-part1 getFormattedTitle:
# e.replace(/[\$_|`$\-+:,\/#&\[\]@\{\}*%.()?–']/g, "-")
_SLUG_CHARS = re.compile(r"[\$`_|\-\+:,\/#&\[\]@\{\}*%.()?–']")


def get_formatted_title(title):
    """Replicate the shipped Phenom title slug (isUrlLowerForSeo=false)."""
    if not title:
        return ""
    value = _SLUG_CHARS.sub("-", str(title))
    value = value.replace(" ", "-")
    value = re.sub(r"-+", "-", value)
    if value.endswith("-"):
        value = value[:-1]
    return value


def native_job_url(base_url, job_seq_no, title):
    """Build the observed job/:jobSeqNo/:title native URL for one posting."""
    if not job_seq_no or not str(job_seq_no).strip():
        raise ValueError("Baker Hughes native job URL requires a jobSeqNo")
    slug = get_formatted_title(title)
    if not slug:
        raise ValueError("Baker Hughes native job URL has an empty title slug")
    if not str(base_url).endswith("/"):
        raise ValueError("Baker Hughes native base URL must end with /")
    return f"{base_url}job/{job_seq_no}/{slug}"


def _clean_row(row):
    return {
        key: value
        for key, value in row.items()
        if key not in _DENY_KEYS
        and not key.startswith("ml_")
        and not key.startswith("emb_")
    }


def attachment_links(description_html):
    """Return role-attachment links published inside a full description."""
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append(
                {"url": href, "label": anchor.get_text(" ", strip=True) or None}
            )
    return values


class BakerHughes(Adapter):
    VERIFICATION_REVISION = VERIFICATION_REVISION

    def evidence(self, url, data, *, body=None):
        """Archive a cleaned public job payload alongside the request log."""
        if not self.http.output:
            return
        encoded = json.dumps(data, ensure_ascii=False).encode()
        digest = sha256(encoded).hexdigest()
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(encoded))
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "url": url,
                        "final_url": url,
                        "method": "POST" if body else "GET",
                        "request_body": body,
                        "status": 200,
                        "sha256": digest,
                        "fetched_at": now(),
                        "evidence_scope": (
                            "public job fields; truncated teasers, generated ML "
                            "summaries, hashes and unrelated personnel omitted"
                        ),
                    }
                )
                + "\n"
            )

    async def listing(self, api, offset):
        """Fetch one unfiltered refineSearch page (UI size 10)."""
        if not self.page_guard():
            raise ValueError(
                "Baker Hughes native listing page budget exhausted"
            )
        body = {
            "lang": self.config["locale"],
            "deviceType": self.config["deviceType"],
            "country": self.config["country"],
            "pageName": self.config["pageName"],
            "ddoKey": "refineSearch",
            "from": offset,
            "size": 10,
            "jobs": True,
            "counts": True,
            "global": True,
            "selected_fields": {},
            "all_fields": [
                "city",
                "state",
                "category",
                "type",
                "orgFunction",
                "country",
            ],
            "clearAll": True,
            "keywords": "",
            "subsearch": "",
            "sortBy": "",
        }
        response = await api.json(
            self.endpoint,
            method="POST",
            json=body,
            headers={"Referer": self.search_entry},
        )
        value = (
            response.get("refineSearch") if isinstance(response, dict) else None
        )
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or not isinstance(value.get("totalHits"), int)
            or value["totalHits"] < 0
            or not isinstance(value.get("data"), dict)
            or not isinstance(value["data"].get("jobs"), list)
        ):
            raise ValueError(
                "Baker Hughes native search returned an error, counts-only "
                "result, or an unknown schema"
            )
        jobs = value["data"]["jobs"]
        if len(jobs) > 10 or value.get("hits") != len(jobs):
            raise ValueError(
                "Baker Hughes native listing hit count does not reconcile"
            )
        clean = [_clean_row(job) for job in jobs]
        aggregations = value["data"].get("aggregations") or []
        self.evidence(
            self.endpoint,
            {
                "refineSearch": {
                    "status": 200,
                    "hits": len(clean),
                    "totalHits": value["totalHits"],
                    "data": {
                        "jobs": clean,
                        "aggregations": aggregations,
                        "SEARCH_CONFIG": value["data"].get("SEARCH_CONFIG"),
                        "suggestions": value["data"].get("suggestions"),
                        "ui_skills_selection": value["data"].get(
                            "ui_skills_selection"
                        ),
                        "spellCheckData": value["data"].get("spellCheckData"),
                    },
                }
            },
            body=body,
        )
        return clean, value["totalHits"]

    async def detail(self, api, job):
        """Fetch the full description for one exact listed jobSeqNo."""
        body = {
            "lang": self.config["locale"],
            "deviceType": self.config["deviceType"],
            "country": self.config["country"],
            "pageName": "job",
            "ddoKey": "jobDetail",
            "jobSeqNo": job.raw["listing"]["jobSeqNo"],
        }
        response = await api.json(
            self.endpoint,
            method="POST",
            json=body,
            headers={"Referer": self.search_entry},
        )
        value = (
            response.get("jobDetail") if isinstance(response, dict) else None
        )
        data = value.get("data", {}).get("job") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or value.get("hits") != 1
            or not isinstance(data, dict)
            or data.get("jobId") != job.job_id
            or data.get("jobSeqNo") != job.raw["listing"]["jobSeqNo"]
            or data.get("refNum") != "BAHUGLOBAL"
            or data.get("title") != job.title
            or not str(data.get("companyName", "")).casefold().startswith(
                "baker hughes"
            )
        ):
            raise ValueError(
                "Baker Hughes full native detail identity does not match "
                "the company-listed posting"
            )
        listing_apply = job.raw["listing"].get("applyUrl")
        detail_apply = data.get("applyUrl")
        if listing_apply and detail_apply and listing_apply != detail_apply:
            raise ValueError(
                f"{job.job_id}: listing/detail apply destination changed"
            )
        listing_posted = job.raw["listing"].get("postedDate")
        detail_posted = data.get("postedDate")
        if listing_posted and detail_posted and listing_posted != detail_posted:
            raise ValueError(
                f"{job.job_id}: listing/detail posting date changed"
            )
        clean = _clean_row(data)
        description = clean.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"{job.job_id}: full native description field is missing"
            )
        self.evidence(
            self.endpoint,
            {"jobDetail": {"status": 200, "hits": 1, "totalHits": 1, "data": {"job": clean}}},
            body=body,
        )
        links = attachment_links(description)
        job.raw["detail"] = clean
        job.raw["native_detail_url"] = job.url
        job.raw["attachment_links"] = links
        if links:
            job.warnings.append("role_attachment_links_present")
            self.result.errors.append(
                f"{job.job_id}: published role attachment links are present; "
                "full attachment text was not captured"
            )
        job.description_html = description
        job.description_method = (
            "baker_hughes.native_widgets.jobDetail.description"
        )
        job.description_source_url = self.endpoint
        multi = clean.get("multi_location")
        if isinstance(multi, list) and multi:
            job.locations = multi
        elif clean.get("location"):
            job.locations = [{"location": clean.get("location")}]
        else:
            job.locations = []
        job.employment_type = clean.get("type")
        job.workplace_type = clean.get("remote")
        job.department = clean.get("category")
        job.team = clean.get("subCategory")
        job.experience = clean.get("experience")
        job.requisition_id = (
            clean.get("jobRequisitionId") or clean.get("reqId")
        )
        job.date_posted = clean.get("postedDate") or job.date_posted
        job.date_posted_raw = clean.get("postedDate")
        job.date_updated = clean.get("jobUpdatedDate") or None
        job.valid_through = clean.get("jobPostingEndDate") or None
        job.employer = clean.get("companyName")
        job.language = clean.get("locale")
        job.apply_url = clean.get("applyUrl") or listing_apply
        return job

    async def _paginate(self, api, *, label):
        """Enumerate refineSearch pages; reconcile by set (relevance order)."""
        rows_by_id = {}
        pages = []
        total = None
        offset = 0
        while True:
            rows, page_total = await self.listing(api, offset)
            if total is None:
                total = page_total
            elif page_total != total:
                self.result.errors.append(
                    f"baker_hughes_native_total_changed_during_{label}_pagination"
                )
            pages.append(
                {
                    "label": label,
                    "from": offset,
                    "hits": len(rows),
                    "totalHits": page_total,
                    "job_ids": [row["jobId"] for row in rows],
                }
            )
            for row in rows:
                identity = row.get("jobId")
                sequence = row.get("jobSeqNo")
                if not identity or not sequence:
                    raise ValueError(
                        "Baker Hughes native listing ID missing for one posting"
                    )
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        raise ValueError(
                            f"Baker Hughes native listing repeated ID "
                            f"{identity} with a changed row"
                        )
                    pages[-1].setdefault("duplicate_ids", []).append(identity)
                    continue
                rows_by_id[identity] = row
            if not rows and len(rows_by_id) < (total or 0):
                self.result.errors.append(
                    f"baker_hughes_native_pagination_ended_early_{label}"
                )
                break
            if len(rows_by_id) >= (total or 0):
                break
            offset += len(rows)
        return rows_by_id, total, pages

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        self.search_entry = str(entry.url)
        parsed = urlsplit(self.search_entry)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "careers.bakerhughes.com"
            or parsed.path != "/global/en/search-results"
        ):
            raise ValueError(
                "Baker Hughes flow must start from its official HTTPS "
                "/global/en/search-results entry"
            )
        entry_waf = None
        try:
            self.config = embedded_page_object(
                entry.text, "var phApp = phApp || "
            )
        except ValueError:
            if "_Incapsula_Resource" not in entry.text:
                raise
            # The data source (widgets) is unblocked; only the HTML shell is
            # challenged. Escalate to the stealth browser and re-read the live
            # phApp config before falling back to the archived same-day copy.
            entry_waf = {
                "url": str(entry.url),
                "status": entry.status_code,
                "challenge": "Incapsula _Incapsula_Resource interstitial",
                "bytes": len(entry.content),
            }
            page = await access.fetch_with_stealth(str(entry.url))
            entry_waf["access"] = access.attempt_metadata(page, url=str(entry.url))
            if page is not None and page.ok:
                try:
                    self.config = embedded_page_object(page.html, "var phApp = phApp || ")
                    entry_waf["escalation"] = "resolved_live_config"
                except ValueError:
                    entry_waf["escalation"] = "resolved_but_no_live_config"
            if "escalation" not in entry_waf:
                entry_waf["escalation"] = "archived_fallback"
                self.config = dict(
                    _ARCHIVED_ENTRY_CONFIG,
                    _provenance=(
                        "archived official entry companies/baker-hughes/research/"
                        "raw/official-search-results-20261107.html "
                        "(fetched 2026-11-07T08:59Z, pre-challenge)"
                    ),
                )
                self.result.errors.append(
                    "baker_hughes_entry_waf_fallback_to_archived_config"
                )
            self.result.metadata["entry_waf"] = entry_waf
        self.endpoint = self.config.get("widgetApiEndpoint")
        self.base = self.config.get("baseUrl", "")
        if (
            self.config.get("refNum") != "BAHUGLOBAL"
            or self.endpoint != "https://careers.bakerhughes.com/widgets"
            or self.config.get("siteType") != "external"
            or self.base != "https://careers.bakerhughes.com/global/en/"
            or self.config.get("pageName") != "search-results"
        ):
            raise ValueError(
                "Baker Hughes native company/search configuration changed"
            )
        ddo = {}
        eager_ids = []
        if entry_waf is None:
            ddo = embedded_page_object(entry.text, "phApp.ddo = ")
            eager = ddo.get("eagerLoadRefineSearch", {})
            eager_jobs = (
                eager.get("data", {}).get("jobs", [])
                if isinstance(eager, dict)
                else []
            )
            eager_ids = [row.get("jobId") for row in eager_jobs]
        async with HTTP(
            None,
            delay=self.http.delay,
            timeout=self.http.client.timeout,
            retries=self.http.retries,
            max_response_bytes=self.http.max_response_bytes,
        ) as api:
            self.result.metadata.update(
                verification_revision=self.VERIFICATION_REVISION,
                official_entry=str(entry.url),
                native_search_entry=self.search_entry,
                listing_authority=(
                    "Baker Hughes native unfiltered global search "
                    "(refineSearch clearAll, global, no keyword); "
                    "not the Workday account/login link"
                ),
                eager_load_job_ids=eager_ids,
                entry_waf=entry_waf,
                entry_config_fallback_used=entry_waf is not None,
            )
            initial_rows, total, initial_pages = await self._paginate(
                api, label="initial"
            )
            self.result.expected_total = total
            if not initial_rows:
                self.result.errors.append(
                    "Baker Hughes official embedded listing returned zero "
                    "visible jobs; native empty state requires independent "
                    "review"
                )
                return self.result
            first_ids = set(initial_pages[0]["job_ids"])
            if eager_ids and set(eager_ids) != first_ids:
                self.result.errors.append(
                    "baker_hughes_eager_first_page_differs_from_first_widgets_page"
                )
            for identity, row in initial_rows.items():
                url = native_job_url(self.base, row["jobSeqNo"], row["title"])
                self.add(
                    self.job(
                        {"listing": row},
                        title=row["title"],
                        job_id=identity,
                        url=url,
                        date_posted=row.get("postedDate"),
                    )
                )
                if self.limit():
                    break
            self.result.metadata["initial_pages"] = initial_pages
            if "sample_limit" in self.result.warnings:
                self.result.listing_complete = False
                self.result.metadata["company_published_job_ids"] = sorted(
                    initial_rows
                )
                return self.result
            self.result.jobs = await self.hydrate(
                self.result.jobs, lambda job: self.detail(api, job)
            )
            final_rows, final_total, final_pages = await self._paginate(
                api, label="final"
            )
            self.result.metadata["final_pages"] = final_pages
            changed_rows = sorted(
                identity
                for identity in set(final_rows) & set(initial_rows)
                if final_rows[identity] != initial_rows[identity]
            )
            if changed_rows:
                self.result.errors.append(
                    "baker_hughes_listing_rows_changed_between_snapshots"
                )
            if final_total != total or set(final_rows) != set(initial_rows):
                self.result.listing_complete = False
                self.result.errors.append(
                    "baker_hughes_native_listing_id_set_changed"
                )
            duplicate_initial = [
                identity for page in initial_pages for identity in page.get("duplicate_ids", [])
            ]
            duplicate_final = [
                identity for page in final_pages for identity in page.get("duplicate_ids", [])
            ]
            self.result.metadata.update(
                initial_job_ids=sorted(initial_rows),
                final_job_ids=sorted(final_rows),
                initial_total=total,
                final_total=final_total,
                duplicate_ids_initial=sorted(set(duplicate_initial)),
                duplicate_ids_final=sorted(set(duplicate_final)),
                changed_rows_between_snapshots=changed_rows,
                evergreen_job_ids=sorted(
                    job.job_id
                    for job in self.result.jobs
                    if (job.raw.get("detail") or {}).get("isEverGreenReq") == "1"
                ),
                test_job_ids=sorted(
                    identity
                    for identity, row in initial_rows.items()
                    if "test" in (row.get("title") or "").casefold()
                ),
            )
            self.result.listing_complete = (
                not self.result.errors
                and len(initial_rows) == total
                and final_total == total
                and set(final_rows) == set(initial_rows)
                and "sample_limit" not in self.result.warnings
            )
            self.result.metadata["company_published_job_ids"] = sorted(
                initial_rows
            )
        return self.result
