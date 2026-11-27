"""Hewlett Packard Enterprise's own Phenom careers search and job pages.

Membership authority is HPE's own unfiltered global ``refineSearch`` response
on ``careers.hpe.com`` — the listing reached from the official entry
``https://careers.hpe.com/us/en/search-results`` — not a guessed ATS slug and
not the Workday tenant that only serves the per-job apply link the company
itself publishes. Full descriptions come from each exact native job page's
``phApp.ddo.jobDetail`` object; teasers, ML/AI summaries and page footers are
never treated as descriptions.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html
from ..structured import embedded_page_object as page_object

HOST = "careers.hpe.com"
ENTRY_PATH = "/us/en/search-results"
BASE_URL = "https://careers.hpe.com/us/en/"
WIDGET_ENDPOINT = "https://careers.hpe.com/widgets"
REFNUM = "HPE1US"
PAGE_SIZE = 10
SITEMAP_INDEX = "https://careers.hpe.com/us/en/sitemap_index.xml"
SITEMAP_FILE_PREFIX = "https://careers.hpe.com/us/en/sitemap"
SITEMAP_FILES = 4
JOB_PATH = re.compile(r"^/us/en/job/(\d+)(?:/([^/]+))?/?$")
JOB_LOC = re.compile(r"^https://careers\.hpe\.com/us/en/job/(\d+)/(.+)$")
SEQ = re.compile(r"^HPE1US(\d+)EXTERNALENUS$")
POSTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}$")
CANONICAL = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.I)
TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)
POLICY_ATTACHMENT = re.compile(
    r"(?:equal[-_ ]?employment|eeo|eoe|privacy|fraud|benefits?|policy|handbook|brochure)", re.I
)
SALARY_TRIGGER = re.compile(r"\b(?:salary|wage)\b", re.I)
SALARY_AMOUNT = re.compile(
    r"(?:USD\s*|\$)\s?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:-|–|—|to)\s*(?:USD\s*|\$)?\s?\d[\d,]*(?:\.\d+)?)?",
    re.I,
)
COMPANY_NAMES = frozenset({"hpe", "hewlett packard enterprise", "hewlett-packard enterprise"})

# Generated, truncated or duplicate-of-description fields are not retained as
# authoritative public posting fields; the rest of each source object is kept.
_DENY_KEYS = frozenset(
    {
        "descriptionTeaser",
        "ml_job_parser",
        "ml_Description",
        "ml_description",
        "ai_summary",
        "emb_complete_desc",
        "emb_hash",
        "fesHash",
        "hashKey",
        "cmsJobChatbot",
    }
)

# Observed in the native URL slugs (all 1752 sitemap-matched titles reproduce
# exactly): these characters become "-", a tab becomes "-", an ASCII space
# becomes "-", runs collapse and a single trailing "-" is removed. A no-break
# space, ";" and "—" are kept verbatim by the company's own slug.
_SLUG_CHARS = re.compile(r"[\$`_|\-\+:,\/#&\[\]@\{\}*%.()?–'\"]")


def get_formatted_title(title):
    """Replicate the shipped Phenom title slug observed on careers.hpe.com."""
    if not title:
        return ""
    value = _SLUG_CHARS.sub("-", str(title))
    value = value.replace("\t", "-").replace(" ", "-")
    value = re.sub(r"-+", "-", value)
    if value.endswith("-"):
        value = value[:-1]
    return value


def native_job_url(job_id, title):
    """Build the observed canonical native job URL for one listed posting."""
    if not re.fullmatch(r"\d+", str(job_id or "")):
        raise ValueError("HPE native job URL requires the numeric jobId")
    slug = get_formatted_title(title)
    if not slug:
        raise ValueError("HPE native job URL has an empty title slug")
    return f"{BASE_URL}job/{job_id}/{slug}"


def _clean_row(row):
    return {
        key: value
        for key, value in row.items()
        if key not in _DENY_KEYS and not key.startswith("ml_") and not key.startswith("emb_")
    }


def attachment_links(description_html):
    """Links to role-bearing files published inside the description body."""
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def role_attachment_links(links):
    """Separate role-bearing files from company policy/brochure files.

    The observed HPE description files are corporate artefacts shared by many
    postings, not role attachments: the Equal Employment Opportunity policy
    (``hr.ext.hpe.com/Equal-employment-opportunity-policy.pdf``) and the HPE
    recruitment brochure (``myhperewards.com/pdf/hpe-recruit-brochure-can*.pdf``).
    They are retained as published links but never treated as a missing role
    description section.
    """
    role, policy = [], []
    for link in links:
        haystack = f"{link.get('url') or ''} {link.get('label') or ''}"
        if POLICY_ATTACHMENT.search(haystack):
            policy.append(link)
        else:
            role.append(link)
    return role, policy


def salary_from_text(text):
    """Verbatim published salary/wage paragraph inside a description, if any.

    HPE publishes no structured salary field; some US postings state the range
    in the description body. The exact source text is retained and nothing is
    parsed into a number.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not SALARY_TRIGGER.search(line):
            continue
        block = []
        for candidate in lines[index:index + 3]:
            block.append(candidate)
            if len(block) > 1 and candidate.endswith(".") and SALARY_AMOUNT.search(candidate):
                break
        source = " ".join(block).strip()
        if SALARY_AMOUNT.search(source):
            return {"source_field": "description", "source_text": source, "line": index}
    return None


def parse_search_page(html):
    """Return (phApp config, embedded eager refineSearch snapshot)."""
    config = page_object(html, "var phApp = phApp || ")
    ddo = page_object(html, "phApp.ddo = ")
    eager = ddo.get("eagerLoadRefineSearch")
    if not isinstance(eager, dict) or eager.get("status") != 200:
        raise ValueError("HPE search page has no successful eagerLoadRefineSearch")
    total = eager.get("totalHits")
    rows = (eager.get("data") or {}).get("jobs")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("HPE search page embedded listing schema changed")
    return config, {"total": total, "rows": rows, "eid": eager.get("eid")}


def sitemap_files(html):
    """File URLs published by the company's own sitemap index."""
    files = re.findall(r"<loc>([^<]+)</loc>", html)
    if (
        len(files) != SITEMAP_FILES
        or any(not f.startswith(SITEMAP_FILE_PREFIX) or not f.endswith(".xml") for f in files)
    ):
        raise ValueError(f"HPE sitemap index changed: {files}")
    return files


def sitemap_job_urls(html):
    """(jobId, canonical-ish sitemap URL) pairs published in one sitemap file."""
    pairs = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", html):
        match = JOB_LOC.fullmatch(loc)
        if match:
            pairs.append((match.group(1), loc))
    return pairs


def parse_job_page(html):
    """Return (phApp config, jobDetail wrapper, job object) from a native page."""
    config = page_object(html, "var phApp = phApp || ")
    ddo = page_object(html, "phApp.ddo = ")
    value = ddo.get("jobDetail")
    data = value.get("data", {}).get("job") if isinstance(value, dict) else None
    return config, value, data


def listing_row_problems(row, refnum=REFNUM):
    """Identity problems for one official listing row (empty list when exact)."""
    problems = []
    if not isinstance(row, dict):
        return ["row is not an object"]
    job_id = row.get("jobId")
    sequence = row.get("jobSeqNo")
    if not isinstance(job_id, str) or not job_id.isdigit():
        problems.append("jobId")
    match = SEQ.fullmatch(sequence or "")
    if not match or match.group(1) != str(job_id):
        problems.append("jobSeqNo")
    elif not str(sequence).startswith(refnum):
        problems.append("refNum")
    if not isinstance(row.get("title"), str) or not row["title"].strip():
        problems.append("title")
    if row.get("siteType") != "external" or row.get("visibilityType") != "External":
        problems.append("visibility")
    if str(row.get("reqId")) != str(job_id):
        problems.append("reqId")
    apply_url = row.get("applyUrl")
    if not isinstance(apply_url, str) or not apply_url.startswith("https://"):
        problems.append("applyUrl")
    if not POSTED.fullmatch(str(row.get("postedDate") or "")):
        problems.append("postedDate")
    return problems


def detail_identity_problems(listing, data, refnum=REFNUM):
    """Problems where a full native page does not match its company-listed row."""
    if not isinstance(data, dict):
        return ["jobDetail job object missing"]
    problems = []
    if str(data.get("jobId")) != str(listing.get("jobId")):
        problems.append(f"jobId {data.get('jobId')!r} != {listing.get('jobId')!r}")
    if data.get("jobSeqNo") != listing.get("jobSeqNo"):
        problems.append(f"jobSeqNo {data.get('jobSeqNo')!r} != {listing.get('jobSeqNo')!r}")
    if data.get("refNum") != refnum:
        problems.append(f"refNum {data.get('refNum')!r}")
    if data.get("title") != listing.get("title"):
        problems.append(f"title {data.get('title')!r} != {listing.get('title')!r}")
    if str(data.get("companyName") or "").strip().casefold() not in COMPANY_NAMES:
        problems.append(f"companyName {data.get('companyName')!r}")
    if listing.get("applyUrl") and data.get("applyUrl") and listing["applyUrl"] != data["applyUrl"]:
        problems.append("applyUrl changed")
    if listing.get("postedDate") and data.get("postedDate") and listing["postedDate"] != data["postedDate"]:
        problems.append("postedDate changed")
    return problems


class HewlettPackardEnterprise(Adapter):
    VERIFICATION_REVISION = "hewlett-packard-enterprise.native-phenom-refine-and-job-pages.1"

    def listing_body(self, offset):
        return {
            "lang": self.config["locale"],
            "deviceType": self.config["deviceType"],
            "country": self.config["country"],
            "pageName": self.config["pageName"],
            "ddoKey": "refineSearch",
            "from": offset,
            "size": PAGE_SIZE,
            "jobs": True,
            "counts": True,
            "global": True,
            "selected_fields": {},
            "all_fields": ["city", "state", "category", "type", "orgFunction", "country"],
            "clearAll": True,
            "keywords": "",
            "subsearch": "",
            "sortBy": "",
        }

    async def listing(self, offset):
        """Fetch one unfiltered refineSearch page exactly as the UI does."""
        if not self.page_guard():
            raise ValueError("HPE native listing page budget exhausted")
        body = self.listing_body(offset)
        response = await self.http.json(
            WIDGET_ENDPOINT, method="POST", json=body, headers={"Referer": self.search_entry}
        )
        value = response.get("refineSearch") if isinstance(response, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or not isinstance(value.get("totalHits"), int)
            or value["totalHits"] < 0
            or not isinstance(value.get("data"), dict)
            or not isinstance(value["data"].get("jobs"), list)
        ):
            raise ValueError("HPE native search returned an error, counts-only result, or unknown schema")
        rows = value["data"]["jobs"]
        if value.get("hits") != len(rows) or len(rows) > PAGE_SIZE:
            raise ValueError("HPE native listing hit count does not reconcile")
        for row in rows:
            problems = listing_row_problems(row)
            if problems:
                raise ValueError(f"HPE listing row identity/scope problem: {problems}")
        return [_clean_row(row) for row in rows], value["totalHits"]

    async def sitemap_candidates(self):
        """Company-published job URLs used only as a completeness cross-check."""
        index = await self.http.get(SITEMAP_INDEX)
        if str(index.url) != SITEMAP_INDEX:
            raise ValueError("HPE sitemap index redirected away from its published URL")
        files = sitemap_files(index.text)
        candidates = {}
        counts = []
        for file_url in files:
            page = await self.http.get(file_url)
            if str(page.url) != file_url:
                raise ValueError(f"HPE sitemap file redirected: {file_url}")
            urls = re.findall(r"<loc>([^<]+)</loc>", page.text)
            job_urls = sitemap_job_urls(page.text)
            counts.append({"sitemap": file_url, "urls": len(urls), "job_urls": len(job_urls)})
            for job_id, loc in job_urls:
                candidates.setdefault(job_id, []).append(loc)
        total_job_urls = sum(len(v) for v in candidates.values())
        duplicates = {
            job_id: sorted(set(urls))
            for job_id, urls in candidates.items()
            if len(urls) > 1
        }
        return (
            {job_id: urls[0] for job_id, urls in candidates.items()},
            counts,
            {"job_urls": total_job_urls, "unique_ids": len(candidates), "duplicate_ids": duplicates},
        )

    async def detail(self, job):
        """Fetch the exact native job page and keep its full description."""
        response = await self.http.get(job.url)
        parsed = urlsplit(str(response.url))
        match = JOB_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != HOST
            or not match
            or match.group(1) != job.job_id
        ):
            raise ValueError("HPE posting redirected away from its exact native job URL")
        config, value, data = parse_job_page(response.text)
        listing = job.raw["listing"]
        if config.get("refNum") != REFNUM:
            raise ValueError(f"HPE native page refNum changed: {config.get('refNum')!r}")
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or value.get("hits") != 1
            or not isinstance(data, dict)
        ):
            raise ValueError("HPE native job page has no single jobDetail object")
        problems = detail_identity_problems(listing, data)
        if problems:
            raise ValueError(
                "HPE native detail does not match the company-listed posting: " + "; ".join(problems)
            )
        description = data.get("description")
        description_text = text_html(description)
        if not isinstance(description, str) or len(description_text.strip()) < 100:
            raise ValueError(f"{job.job_id}: full native description field is missing")
        canonical_match = CANONICAL.search(response.text)
        canonical = canonical_match.group(1) if canonical_match else None
        if canonical:
            canonical_match_path = JOB_PATH.fullmatch(urlsplit(canonical).path)
            if (
                urlsplit(canonical).hostname != HOST
                or not canonical_match_path
                or canonical_match_path.group(1) != job.job_id
            ):
                raise ValueError(f"{job.job_id}: native page canonical URL points at another posting")
        clean = _clean_row(data)
        job.raw["detail"] = clean
        job.raw["native_detail_url"] = job.url
        job.raw["canonical_url"] = canonical
        job.description_html = description
        job.description_text = description_text
        job.description_method = "hewlett_packard_enterprise.native_job_page.jobDetail.description"
        job.description_source_url = job.url
        job.title = data.get("title") or job.title
        job.requisition_id = data.get("jobRequisitionId") or data.get("reqId") or job.requisition_id
        job.apply_url = data.get("applyUrl") or job.apply_url
        multi = data.get("multi_location")
        if isinstance(multi, list) and multi:
            job.locations = multi
        elif data.get("location"):
            job.locations = [{"location": data["location"]}]
        else:
            job.locations = []
        job.department = data.get("category")
        job.team = data.get("jobFamily")
        job.employment_type = data.get("type")
        job.workplace_type = data.get("remote")
        job.date_posted = data.get("postedDate") or job.date_posted
        job.date_posted_raw = data.get("postedDate")
        job.date_updated = data.get("jobUpdatedDate") or None
        job.valid_through = None  # HPE publishes no posting deadline field
        job.salary = salary_from_text(description_text)
        job.employer = data.get("jobCompany") or data.get("companyName")
        job.language = data.get("locale")
        evergreen = str(data.get("isEverGreenReq") or "").strip() == "1"
        links = attachment_links(description)
        role_links, policy_links = role_attachment_links(links)
        job.raw["source_ui"] = {
            "listing_jobSeqNo": listing.get("jobSeqNo"),
            "canonical_url": canonical,
            "slug_matches_canonical": canonical == job.url if canonical else None,
            "attachment_links": links,
            "role_attachment_links": role_links,
            "policy_attachment_links": policy_links,
            "attachment_classification": {
                "role": len(role_links),
                "policy_or_brochure": len(policy_links),
            },
            "salary_from_description": job.salary,
            "is_evergreen": evergreen,
            "refNum": data.get("refNum"),
            "ats": data.get("ats"),
        }
        if evergreen:
            job.warnings.append("evergreen_requisition")
        if role_links:
            job.warnings.append("role_attachment_links_present")
            self.result.errors.append(
                f"{job.job_id}: published role attachment links are present; "
                "full attachment text was not captured"
            )
        return job

    async def _paginate(self, label):
        """Enumerate every official listing page and reconcile the ID set."""
        rows_by_id = {}
        pages = []
        total = None
        offset = 0
        while True:
            rows, page_total = await self.listing(offset)
            if total is None:
                total = page_total
            elif page_total != total:
                self.result.errors.append(f"hewlett_packard_enterprise_total_changed_during_{label}")
            pages.append(
                {
                    "from": offset,
                    "hits": len(rows),
                    "totalHits": page_total,
                    "first_id": rows[0]["jobId"] if rows else None,
                    "last_id": rows[-1]["jobId"] if rows else None,
                }
            )
            for row in rows:
                identity = row["jobId"]
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        raise ValueError(f"HPE listing repeated ID {identity} with a changed row")
                    pages[-1]["duplicate_ids"] = pages[-1].get("duplicate_ids", []) + [identity]
                    continue
                rows_by_id[identity] = row
            if not rows and len(rows_by_id) < (total or 0):
                self.result.errors.append(f"hewlett_packard_enterprise_pagination_ended_early_{label}")
                break
            if len(rows_by_id) >= (total or 0):
                break
            offset += len(rows)
        return rows_by_id, total, pages

    async def run(self):
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url)
        self.search_entry = str(entry.url)
        parsed_entry = urlsplit(self.search_entry)
        if (
            parsed_entry.scheme != "https"
            or parsed_entry.hostname != HOST
            or parsed_entry.path != ENTRY_PATH
        ):
            raise ValueError("HPE flow must start from its official HTTPS /us/en/search-results entry")
        self.config, eager = parse_search_page(entry.text)
        self.endpoint = self.config.get("widgetApiEndpoint")
        if (
            self.config.get("refNum") != REFNUM
            or self.endpoint != WIDGET_ENDPOINT
            or self.config.get("siteType") != "external"
            or self.config.get("baseUrl") != BASE_URL
            or self.config.get("pageName") != "search-results"
            or self.config.get("locale") != "en_us"
            or self.config.get("country") != "us"
            or self.config.get("deviceType") != "desktop"
        ):
            raise ValueError("HPE native company/search configuration changed")
        title_match = TITLE_TAG.search(entry.text)
        eager_ids = [row.get("jobId") for row in eager["rows"]]

        listing_started = time.perf_counter()
        initial_rows, total, initial_pages = await self._paginate("initial")
        listing_seconds = time.perf_counter() - listing_started
        self.result.expected_total = total
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=self.search_entry,
            official_entry_title=(title_match.group(1).strip() if title_match else None),
            native_search_entry=self.search_entry,
            widget_api_endpoint=self.endpoint,
            refNum=REFNUM,
            page_size=PAGE_SIZE,
            eager_load_total_hits=eager["total"],
            eager_load_first_page_ids=eager_ids,
            listing_authority=(
                "HPE's own unfiltered global refineSearch on careers.hpe.com "
                "(clearAll=true, global=true, no keyword, no facet); the Workday "
                "tenant only supplies the published per-job apply links"
            ),
            initial_pages=initial_pages,
        )
        if eager["total"] != total:
            self.result.errors.append(
                f"hewlett_packard_enterprise_search_page_total_{eager['total']}_"
                f"differs_from_widget_total_{total}"
            )
        if not initial_rows:
            self.result.errors.append(
                "HPE official listing returned zero visible jobs; the native empty "
                "state requires independent review"
            )
            return self.result
        if eager_ids and len(eager_ids) == initial_pages[0]["hits"] and set(eager_ids) != set(
            list(initial_rows)[: len(eager_ids)]
        ):
            self.result.errors.append(
                "hewlett_packard_enterprise_eager_first_page_differs_from_widgets_first_page"
            )

        sitemap_started = time.perf_counter()
        candidates, sitemap_counts, sitemap_stats = await self.sitemap_candidates()
        sitemap_seconds = time.perf_counter() - sitemap_started
        probe_started = time.perf_counter()
        gone, live_unlisted, probe_failures = [], [], []
        for job_id in sorted(set(candidates) - set(initial_rows)):
            url = candidates[job_id]
            try:
                response = await self.http.get(url)
            except Exception as exc:
                if "HTTP 410" in str(exc) or "HTTP 404" in str(exc):
                    gone.append({"jobId": job_id, "url": url, "status": str(exc)[:40]})
                else:
                    probe_failures.append({"jobId": job_id, "url": url, "error": str(exc)})
                    self.result.errors.append(
                        f"{job_id}: sitemap candidate probe failed inconclusively: {exc}"
                    )
                continue
            _, value, data = parse_job_page(response.text)
            detail_id = (data or {}).get("jobId")
            if detail_id == job_id:
                live_unlisted.append({"jobId": job_id, "url": url})
                self.result.errors.append(
                    f"{job_id}: live company sitemap candidate absent from the official refineSearch listing"
                )
            else:
                probe_failures.append({"jobId": job_id, "url": url, "detail_jobId": detail_id})
                self.result.errors.append(
                    f"{job_id}: sitemap candidate page returned another posting ({detail_id})"
                )
        probe_seconds = time.perf_counter() - probe_started
        listing_only = sorted(set(initial_rows) - set(candidates))

        for identity, row in initial_rows.items():
            self.add(
                self.job(
                    {"listing": row},
                    title=row["title"],
                    job_id=identity,
                    requisition_id=row.get("reqId") or identity,
                    url=native_job_url(identity, row["title"]),
                    apply_url=row.get("applyUrl"),
                    date_posted=row.get("postedDate"),
                    date_posted_raw=row.get("postedDate"),
                )
            )
            if self.limit():
                break
        if "sample_limit" in self.result.warnings:
            self.result.listing_complete = False
            self.result.metadata["company_published_job_ids"] = sorted(initial_rows)
            return self.result

        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)

        final_started = time.perf_counter()
        final_rows, final_total, final_pages = await self._paginate("final")
        final_seconds = time.perf_counter() - final_started
        self.result.metadata["final_pages"] = final_pages
        changed_rows = sorted(
            identity
            for identity in set(final_rows) & set(initial_rows)
            if final_rows[identity] != initial_rows[identity]
        )
        if changed_rows:
            self.result.errors.append("hewlett_packard_enterprise_listing_rows_changed_between_snapshots")
        if final_total != total or set(final_rows) != set(initial_rows):
            self.result.listing_complete = False
            self.result.errors.append("hewlett_packard_enterprise_native_listing_id_set_changed")

        category_counts = {}
        country_counts = {}
        type_counts = {}
        locale_counts = {}
        for row in initial_rows.values():
            category_counts[str(row.get("category"))] = category_counts.get(str(row.get("category")), 0) + 1
            country_counts[str(row.get("country"))] = country_counts.get(str(row.get("country")), 0) + 1
            type_counts[str(row.get("type"))] = type_counts.get(str(row.get("type")), 0) + 1
            locale_counts[str(row.get("locale"))] = locale_counts.get(str(row.get("locale")), 0) + 1
        salaries = [job.job_id for job in self.result.jobs if job.salary]
        canonical_differences = sorted(
            job.job_id
            for job in self.result.jobs
            if (job.raw.get("source_ui") or {}).get("slug_matches_canonical") is False
        )
        evergreen_ids = [
            job.job_id for job in self.result.jobs if (job.raw.get("source_ui") or {}).get("is_evergreen")
        ]
        attachments_total = sum(
            len((job.raw.get("source_ui") or {}).get("attachment_links") or [])
            for job in self.result.jobs
        )
        role_attachments_total = sum(
            len((job.raw.get("source_ui") or {}).get("role_attachment_links") or [])
            for job in self.result.jobs
        )
        policy_attachment_urls = sorted(
            {
                link["url"]
                for job in self.result.jobs
                for link in (job.raw.get("source_ui") or {}).get("policy_attachment_links") or []
                if link.get("url")
            }
        )
        description_chars = [len(job.description_text) for job in self.result.jobs if job.description_text]
        duplicate_initial = [d for page in initial_pages for d in page.get("duplicate_ids", [])]
        duplicate_final = [d for page in final_pages for d in page.get("duplicate_ids", [])]
        self.result.pages = max(1, len(initial_pages))
        self.result.metadata.update(
            initial_job_ids=sorted(initial_rows),
            initial_total=total,
            final_job_ids=sorted(final_rows),
            final_total=final_total,
            duplicate_ids_initial=sorted(set(duplicate_initial)),
            duplicate_ids_final=sorted(set(duplicate_final)),
            changed_rows_between_snapshots=changed_rows,
            category_counts=dict(sorted(category_counts.items(), key=lambda item: -item[1])),
            country_counts=dict(sorted(country_counts.items(), key=lambda item: -item[1])),
            employment_type_counts=type_counts,
            locale_counts=locale_counts,
            country_count=len(country_counts),
            evergreen_job_ids=evergreen_ids,
            salary_source_text_job_ids=salaries,
            attachment_link_count=attachments_total,
            role_attachment_link_count=role_attachments_total,
            policy_attachment_urls=policy_attachment_urls,
            canonical_slug_difference_ids=canonical_differences,
            description_text_chars={
                "min": min(description_chars) if description_chars else 0,
                "max": max(description_chars) if description_chars else 0,
                "jobs": len(description_chars),
            },
            sitemap_index_url=SITEMAP_INDEX,
            sitemap_file_counts=sitemap_counts,
            sitemap_job_urls=sitemap_stats["job_urls"],
            sitemap_unique_ids=sitemap_stats["unique_ids"],
            sitemap_duplicate_ids=sorted(sitemap_stats["duplicate_ids"]),
            sitemap_only_ids=sorted(set(candidates) - set(initial_rows)),
            sitemap_gone=sorted(gone, key=lambda item: item["jobId"]),
            sitemap_live_unlisted=live_unlisted,
            sitemap_probe_failures=probe_failures,
            listed_ids_absent_from_sitemap=listing_only,
            description_source="phApp.ddo.jobDetail.data.job.description on each exact native job page",
            date_policy=(
                "postedDate is the only published posting date; dateCreated is retained "
                "raw and never used as a posting date; no deadline field is published"
            ),
            coverage_notes=(
                "us/en is the unfiltered global listing (49 countries). The ca/fr locale "
                "exposes 1750 rows that are a strict subset of these jobIds, so translated "
                "locale variants are not separate postings."
            ),
        )
        self.result.listing_complete = (
            not self.result.errors
            and len(initial_rows) == total
            and final_total == total
            and set(final_rows) == set(initial_rows)
            and "sample_limit" not in self.result.warnings
        )
        self.result.metadata["company_published_job_ids"] = sorted(initial_rows)
        self.result.timings["entry_seconds"] = round(listing_started - started, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["sitemap_phase_seconds"] = round(sitemap_seconds, 6)
        self.result.timings["sitemap_probe_seconds"] = round(probe_seconds, 6)
        self.result.timings["final_proof_phase_seconds"] = round(final_seconds, 6)
        self.result.timings["collection_wall_seconds"] = round(time.perf_counter() - started, 6)
        return self.result
