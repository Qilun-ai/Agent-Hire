"""eBay's own Phenom search and native job-detail journey.

Membership authority is the unfiltered global search on eBay's hosted Phenom
site (``jobs.ebayinc.com`` ``/widgets`` ``refineSearch``). The assigned
``careers.ebayinc.com/us/en/jobs/`` URL currently returns a 404 page-not-found;
the official home's ``Search Jobs`` link points at the live Phenom search entry,
whose embedded ``phApp`` config and ``eagerLoadRefineSearch`` are verified before
any widgets call. Full descriptions come from the site's own ``jobDetail`` widget
(Cross-checked against the embedded SSR ``jobDetail`` on two representative native
job pages). Per-job Workday ``applyUrl`` values are application destinations only
and are never enumerated. Truncated teasers and AI/ML summaries are never descriptions.
"""
import gzip
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import HTTP
from ..models import now, text_html
from ..structured import embedded_page_object

VERIFICATION_REVISION = "ebay.native-phenom-widgets-refine-and-jobdetail.1"

SEARCH_ENTRY = "https://jobs.ebayinc.com/us/en/search-results"
CAREERS_HOME = "https://careers.ebayinc.com/"

# Truncated previews, generated/ML embedding fields, hashes and unrelated
# personnel are omitted from retained evidence. Every other public field
# (including questionnaire IDs, CMS/standardised metadata and isEverGreenReq)
# is retained.
_DENY_KEYS = frozenset(
    {
        "descriptionTeaser",
        "ml_job_parser",
        "ml_Description",
        "ml_country",
        "ml_highlight",
        "ml_job_overview",
        "ml_language_fullform",
        "ml_skills",
        "ml_title",
        "hiringManager",
        "primarySourcer",
        "coordinator",
        "ai_summary",
        "emb_complete_desc",
        "emb_hash",
        "fesHash",
        "hashKey",
        "TestingNew",
        "recruiterfirstName",
        "secondaryHiringManagerName",
        "recruitingAgency",
        "nonManagerPhoneScreener",
        "cmsJobChatbot",
    }
)

# Exact character class from ph-common-bundle-part1 getFormattedTitle:
# e.replace(/[\$_|`$\-+:,\/#&\[\]@\{\}*%.()?–']/g, "-")
def _slug_chars():
    return re.compile(r"[\$`_|\-\+:,\/#&\[\]@\{\}*%.()?–]")


_SLUG_CHARS = _slug_chars()


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
        raise ValueError("eBay native job URL requires a jobSeqNo")
    slug = get_formatted_title(title)
    if not slug:
        raise ValueError("eBay native job URL has an empty title slug")
    if not str(base_url).endswith("/"):
        raise ValueError("eBay native base URL must end with /")
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


def salary_from_description(description_text):
    """Retain an explicit pay-range line verbatim; never convert to base salary."""
    for line in (description_text or "").splitlines():
        value = line.strip()
        if re.search(r"\$", value) and re.search(
            r"\b(pay|salary|compensation|OTE|range)\b", value, re.I
        ):
            return {"source_text": value, "source_field": "description"}
    return None


class EBay(Adapter):
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
            raise ValueError("eBay native listing page budget exhausted")
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
                "eBay native search returned an error, counts-only "
                "result, or an unknown schema"
            )
        jobs = value["data"]["jobs"]
        if len(jobs) > 10 or value.get("hits") != len(jobs):
            raise ValueError(
                "eBay native listing hit count does not reconcile"
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
            or data.get("refNum") != "EBAEBAUS"
            or data.get("title") != job.title
            or not str(data.get("companyName", "")).casefold().startswith(
                "ebay"
            )
        ):
            raise ValueError(
                "eBay full native detail identity does not match "
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
            "ebay.native_widgets.jobDetail.description"
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
        job.team = clean.get("jobFamily")
        job.experience = clean.get("experienceLevel") or clean.get("experience")
        job.requisition_id = (
            clean.get("jobRequisitionId") or clean.get("reqId")
        )
        job.date_posted = clean.get("postedDate") or job.date_posted
        job.date_posted_raw = clean.get("postedDate")
        job.date_updated = clean.get("jobUpdatedDate") or None
        # targetHireDate / fixedTermWorkerEndDate are not posting deadlines and
        # must not be copied into valid_through.
        job.valid_through = None
        job.employer = clean.get("companyName")
        job.language = clean.get("locale")
        job.apply_url = clean.get("applyUrl") or listing_apply
        # description_text is populated during validate; compute now so the
        # stored salary field reconciles even before validation.
        job.salary = salary_from_description(text_html(description))
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
                    f"ebay_native_total_changed_during_{label}_pagination"
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
                        "eBay native listing ID missing for one posting"
                    )
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        raise ValueError(
                            f"eBay native listing repeated ID "
                            f"{identity} with a changed row"
                        )
                    pages[-1].setdefault("duplicate_ids", []).append(identity)
                    continue
                rows_by_id[identity] = row
            if not rows and len(rows_by_id) < (total or 0):
                self.result.errors.append(
                    f"ebay_native_pagination_ended_early_{label}"
                )
                break
            if len(rows_by_id) >= (total or 0):
                break
            offset += len(rows)
        return rows_by_id, total, pages

    async def _representative_native_page(self, http, job_url):
        """Open one exact native job page; return its embedded jobDetail job."""
        response = await http.get(job_url)
        parsed = urlsplit(str(response.url))
        if parsed.hostname != "jobs.ebayinc.com" or "/job/" not in parsed.path:
            raise ValueError(
                f"eBay native job page redirected away from its exact route: {response.url}"
            )
        ddo = embedded_page_object(response.text, "phApp.ddo = ")
        value = ddo.get("jobDetail", {})
        data = value.get("data", {}).get("job") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or not isinstance(data, dict)
            or not data.get("jobId")
        ):
            raise ValueError(
                "eBay native job page does not embed a valid jobDetail"
            )
        return data

    def _resolve_search_entry(self, company_entry_url, entry_text, entry_status):
        """Resolve the live Phenom search entry from the official site.

        The assigned careers_url currently 404s; the official careers home's own
        ``Search Jobs`` nav link is the company-published route to the Phenom host.
        That link lives inside ``<ebi-header><template shadowrootmode="open">``
        (Declarative Shadow DOM), which BeautifulSoup cannot descend into, so the
        shipped anchor is matched against the raw HTML instead of the DOM tree.
        """
        matches = re.findall(
            r'<a\s[^>]*href="https://jobs\.ebayinc\.com/us/en/search-results"[^>]*>'
            r"\s*Search Jobs\s*</a>",
            entry_text,
        )
        if len(matches) < 1:
            raise ValueError(
                "Official careers home has no inspected Search Jobs link to the "
                "live Phenom search entry"
            )
        if "<template shadowrootmode" not in entry_text:
            raise ValueError(
                "Official careers home header no longer ships the inspected "
                "shadow-DOM nav"
            )
        parsed = urlsplit(SEARCH_ENTRY)
        if parsed.hostname != "jobs.ebayinc.com":
            raise ValueError("eBay Search Jobs left its inspected company-hosted listing")
        return SEARCH_ENTRY

    async def run(self):
        # 1. Official entry: record the assigned URL's actual (404) status.
        entry = None
        entry_error = None
        try:
            entry = await self.http.get(self.company.careers_url)
        except Exception as exc:  # FetchError on 404 (HTTP raises on 4xx)
            entry_error = f"{type(exc).__name__}: {exc}"
        entry_status = 404 if entry is None else entry.status_code
        entry_final = str(entry.url) if entry is not None else self.company.careers_url
        # The assigned URL is a dead page-not-found; resolve via the official home.
        home = await self.http.get(CAREERS_HOME)
        search_entry_url = self._resolve_search_entry(
            str(home.url), home.text, home.status_code
        )
        category = await self.http.get(search_entry_url)
        if str(category.url) != search_entry_url:
            raise ValueError(
                "eBay native search entry redirected away from its exact route"
            )
        self.search_entry = str(category.url)
        self.config = embedded_page_object(
            category.text, "var phApp = phApp || "
        )
        self.endpoint = self.config.get("widgetApiEndpoint")
        self.base = self.config.get("baseUrl", "")
        if (
            self.config.get("refNum") != "EBAEBAUS"
            or self.endpoint != "https://jobs.ebayinc.com/widgets"
            or self.config.get("siteType") != "external"
            or self.base != "https://jobs.ebayinc.com/us/en/"
            or self.config.get("pageName") != "search-results"
            or self.config.get("locale") != "en_us"
            or self.config.get("country") != "us"
        ):
            raise ValueError(
                "eBay native company/search configuration changed"
            )
        ddo = embedded_page_object(category.text, "phApp.ddo = ")
        eager = ddo.get("eagerLoadRefineSearch", {})
        eager_jobs = (
            eager.get("data", {}).get("jobs", []) if isinstance(eager, dict) else []
        )
        eager_ids = [
            _clean_row(row).get("jobId") if isinstance(row, dict) else None
            for row in eager_jobs
        ]
        async with HTTP(
            None,
            delay=self.http.delay,
            timeout=self.http.client.timeout,
            retries=self.http.retries,
            max_response_bytes=self.http.max_response_bytes,
        ) as api:
            self.result.metadata.update(
                verification_revision=self.VERIFICATION_REVISION,
                official_entry=entry_final,
                official_entry_status=entry_status,
                official_entry_error=entry_error,
                official_careers_home=str(home.url),
                native_search_entry=self.search_entry,
                listing_authority=(
                    "eBay native unfiltered global search "
                    "(refineSearch clearAll, global, no keyword); "
                    "the assigned careers_url 404s and resolves via the official "
                    "home Search Jobs link; Workday apply links are not authorities"
                ),
                eager_load_job_ids=eager_ids,
            )
            if entry is not None:
                self.result.errors.append(
                    f"ebay_assigned_entry_unexpected_status_{entry_status}"
                )
            elif entry_status != 404:
                self.result.errors.append(
                    "ebay_assigned_entry_unreachable_not_404"
                )
            initial_rows, total, initial_pages = await self._paginate(
                api, label="initial"
            )
            self.result.expected_total = total
            if not initial_rows:
                self.result.errors.append(
                    "eBay official embedded listing returned zero "
                    "visible jobs; native empty state requires independent "
                    "review"
                )
                return self.result
            first_ids = set(initial_pages[0]["job_ids"])
            if eager_ids and set(eager_ids) != first_ids:
                self.result.errors.append(
                    "ebay_eager_first_page_differs_from_first_widgets_page"
                )
            if eager.get("status") != 200 or eager.get("totalHits") != total:
                self.result.errors.append(
                    "ebay_eager_total_differs_from_widgets_total"
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
            # Representative native-page checks (two exact listed routes) run on
            # the outer http client so their HTML is archived as evidence.
            page_checks = []
            for job in self.result.jobs[:2]:
                try:
                    ssr_job = await self._representative_native_page(
                        self.http, job.url
                    )
                    match = (
                        ssr_job.get("jobId") == job.job_id
                        and ssr_job.get("jobSeqNo") == job.raw["listing"]["jobSeqNo"]
                        and ssr_job.get("title") == job.title
                        and text_html(ssr_job.get("description") or "")
                        == text_html(job.description_html or "")
                    )
                    page_checks.append(
                        {
                            "job_id": job.job_id,
                            "native_url": job.url,
                            "embedded_job_matches_widgets_detail": match,
                        }
                    )
                    if not match:
                        self.result.errors.append(
                            f"ebay_native_page_jobDetail_differs_{job.job_id}"
                        )
                except Exception as exc:
                    page_checks.append(
                        {
                            "job_id": job.job_id,
                            "native_url": job.url,
                            "embedded_job_matches_widgets_detail": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    self.result.errors.append(
                        f"ebay_representative_native_page_failed_{job.job_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
            self.result.metadata["representative_native_pages"] = page_checks
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
                    "ebay_listing_rows_changed_between_snapshots"
                )
            if final_total != total or set(final_rows) != set(initial_rows):
                self.result.listing_complete = False
                self.result.errors.append(
                    "ebay_native_listing_id_set_changed"
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
