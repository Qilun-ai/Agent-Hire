"""Mastercard's own Phenom search listing and native job-detail journey.

Membership authority is the unfiltered global search published on Mastercard's
own career site (``careers.mastercard.com/us/en/search-results``). The site's
``phApp`` config, its SSR ``eagerLoadRefineSearch`` bootstrap and its own
``POST /widgets`` ``refineSearch`` request carry the same ``totalHits`` (951 at
review time) and the same global, unfiltered scope. The rendered UI pages the
same request five rows at a time; its session variant
(``eagerLoadRefineSearchSession``) is browser-token gated, so this adapter uses
the SSR-observed ``refineSearch`` variant with the server-accepted page size 500
and accumulates rows across passes until the distinct ``jobId`` set equals
``totalHits`` (the relevance order can rotate between snapshots).

Full descriptions come from the site's own ``POST /widgets`` ``jobDetail``
variant for each exact listed ``jobSeqNo``; that object is the same
``phApp.ddo.jobDetail.data.job`` Mastercard's native job page embeds and renders.
Representative native job pages are still fetched and their embedded body is
compared byte-for-byte with the stored description. The company sitemap is used
only for exact canonical job URLs and to detect stale candidates (HTTP 410), not
to add or remove membership. Workday ``applyUrl`` values are application
destinations only and are never enumerated.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from ..models import now, text_html
from ..structured import embedded_page_object
from .base import Adapter

VERIFICATION_REVISION = "mastercard.native-phenom-refine-search-and-job-detail.1"

ENTRY = "https://careers.mastercard.com/us/en/search-results"
BASE_URL = "https://careers.mastercard.com/us/en/"
WIDGET_ENDPOINT = "https://careers.mastercard.com/widgets"
APPLY_ROUTE = "https://careers.mastercard.com/us/en/apply?jobSeqNo="
SITEMAP_INDEX = "https://careers.mastercard.com/us/en/sitemap_index.xml"
REFNUM = "MASRUS"
SITE_TYPE = "external"
LOCALE = "en_us"
COUNTRY = "us"
DEVICE = "desktop"
PAGE_NAME = "search-results"
PAGE_SIZE = 500
MAX_LISTING_PASSES = 6
MAX_FINAL_PROOF_ATTEMPTS = 3

JOB_ID = re.compile(r"^R-(\d{6})$")
JOB_SEQ = re.compile(r"^MASRUSR(\d{6})EXTERNALENUS$")
SITEMAP_JOB = re.compile(r"/job/(R-\d{6})/([^/?#]+)")
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)

# Generated/derived payloads (ML features, embeddings, teasers, hashes, chunk
# vectors and the schema.org copy of the same description) are not public job
# fields and are omitted from retained evidence. Everything else is kept.
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
        "ai_summary",
        "emb_complete_desc",
        "emb_hash",
        "hashKey",
        "structureData",
        "metadata",
    }
)

# Exact character class from the shipped Phenom getFormattedTitle helper:
# e.replace(/[\$_|`$\-+:,\/#&\[\]@\{\}*%.()?–']/g, "-")
_SLUG_CHARS = re.compile(r"[\$_|`$\-+:,\\/#&\[\]@\{\}*%.()?–']")

MONEY = re.compile(r"(?:[$€£₹]\s?\d[\d,.]*|\b(?:USD|CAD|EUR|GBP|INR)\s?\d[\d,.]*)", re.I)
MONEY_RANGE = re.compile(
    r"(?:[$€£₹]\s?\d[\d,.]*\s*(?:-|–|to)\s*(?:[$€£₹]\s?)?\d[\d,.]+"
    r"|(?:USD|CAD|EUR|GBP|INR)\s?\d[\d,.]*\s*(?:-|–|to)\s*(?:USD|CAD|EUR|GBP|INR)?\s?\d[\d,.]+)",
    re.I,
)
PAY_WORDS = re.compile(r"\b(pay|salary|compensation|hourly|per hour|per annum|OTE)\b", re.I)

RESPONSIBILITY_MARKERS = (
    "Key Responsibilities",
    "Key Accountabilities",
    "Responsibilities",
    "All About the Role",
    "In this role, you will",
    "Your Responsibilities",
    "What You'll Do",
    "Role",
)
QUALIFICATION_MARKERS = (
    "All About You",
    "Qualifications",
    "Basic qualifications",
    "Preferred qualifications",
    "The ideal candidate",
    "What You Bring",
    "Requirements",
    "Experience and Skills",
)
GENERAL_SECTION_STOPS = (
    "Corporate Security Responsibility",
    "Our Purpose",
    "Title and Summary",
    "Overview",
)
RESPONSIBILITY_STOPS = GENERAL_SECTION_STOPS + QUALIFICATION_MARKERS
QUALIFICATION_STOPS = GENERAL_SECTION_STOPS + RESPONSIBILITY_MARKERS


def get_formatted_title(title):
    """Replicate the shipped Phenom title slug used in Mastercard card links."""
    if not title:
        return ""
    value = _SLUG_CHARS.sub("-", str(title))
    value = value.replace(" ", "-")
    value = re.sub(r"-+", "-", value)
    if value.endswith("-"):
        value = value[:-1]
    return value


def native_job_url(job_id, title):
    """Build the exact ``/job/{jobId}/{titleSlug}`` route observed in the UI."""
    if not job_id or not JOB_ID.fullmatch(str(job_id)):
        raise ValueError(f"Mastercard job URL requires an R-number id, got {job_id!r}")
    slug = get_formatted_title(title)
    if not slug:
        raise ValueError(f"Mastercard job URL for {job_id} has an empty title slug")
    return f"{BASE_URL}job/{job_id}/{slug}"


def apply_route_url(job_seq_no):
    """Exact company-hosted Apply Now control target (never fetched)."""
    if not job_seq_no:
        raise ValueError("Mastercard apply route requires the listed jobSeqNo")
    return APPLY_ROUTE + str(job_seq_no)


def _clean(row):
    return {
        key: value
        for key, value in row.items()
        if key not in _DENY_KEYS
        and not key.startswith("ml_")
        and not key.startswith("emb_")
        and not key.startswith("completeDescription_chunk_")
    }


def attachment_links(description_html):
    """Role attachments published inside the full description body."""
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def salary_from_description(description_text):
    """Retain an explicit published pay range verbatim; never infer one."""
    for line in (description_text or "").splitlines():
        value = line.strip()
        if not value or not MONEY.search(value):
            continue
        if MONEY_RANGE.search(value) or PAY_WORDS.search(value):
            return {"source_text": value, "source_field": "description"}
    return None


def _is_stop(line, stops):
    value = line.strip().rstrip(":").strip().lower()
    if not value:
        return False
    for stop in stops:
        marker = stop.lower()
        if value == marker:
            return True
        if len(line.strip()) < 100 and value.startswith(marker):
            return True
    return False


def _excerpt(text, markers, stops):
    """First verbatim block for a section marker, honouring marker priority."""
    lines = (text or "").split("\n")
    for marker in markers:
        lower = marker.lower()
        start = None
        for index, line in enumerate(lines):
            value = line.strip().rstrip(":").strip().lower()
            if value == lower:
                start = index + 1
                break
            if value.startswith(lower) and len(line.strip()) > len(marker) + 2:
                start = index
                break
        if start is None:
            continue
        end = len(lines)
        for following in range(start + 1, len(lines)):
            if _is_stop(lines[following], stops):
                end = following
                break
        block = "\n".join(lines[start:end]).strip()
        # The highest-priority marker wins; a too-short block is reported as
        # absent instead of silently falling back to a weaker heading.
        return block if len(block) >= 40 else None
    return None


def sections_from_description(description_text):
    """Return (qualifications, responsibilities) verbatim excerpts or None."""
    text = description_text or ""
    return (
        _excerpt(text, QUALIFICATION_MARKERS, QUALIFICATION_STOPS),
        _excerpt(text, RESPONSIBILITY_MARKERS, RESPONSIBILITY_STOPS),
    )


def normalised_body_text(text):
    """Normalisation used for the independent rendered-body comparison."""
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def fnv1a64(text):
    digest = 0xCBF29CE484222325
    for byte in (text or "").encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def parse_app_config(html):
    return embedded_page_object(html, "var phApp = phApp || ")


def parse_page_ddo(html):
    return embedded_page_object(html, "phApp.ddo = ")


def parse_eager_listing(html):
    """Return (ids, total) from the search page's embedded first page."""
    eager = parse_page_ddo(html).get("eagerLoadRefineSearch", {})
    if not isinstance(eager, dict) or eager.get("status") != 200:
        raise ValueError("Mastercard search page has no valid eagerLoadRefineSearch")
    total = eager.get("totalHits")
    jobs = (eager.get("data") or {}).get("jobs")
    if not isinstance(total, int) or not isinstance(jobs, list):
        raise ValueError("Mastercard eager listing schema changed")
    if eager.get("hits") != len(jobs):
        raise ValueError("Mastercard eager listing hit count does not reconcile")
    ids = [row.get("jobId") for row in jobs if isinstance(row, dict)]
    if len(ids) != len(jobs) or not all(JOB_ID.fullmatch(value or "") for value in ids):
        raise ValueError("Mastercard eager listing contains a row without an R-number jobId")
    return ids, total


def parse_listing_response(payload):
    """Validate one company-scoped refineSearch response -> (rows, total)."""
    value = payload.get("refineSearch") if isinstance(payload, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("status") != 200
        or not isinstance(value.get("totalHits"), int)
        or value["totalHits"] < 0
        or not isinstance(value.get("data"), dict)
        or not isinstance(value["data"].get("jobs"), list)
    ):
        raise ValueError("Mastercard refineSearch returned an unknown schema or error")
    rows = value["data"]["jobs"]
    if value.get("hits") != len(rows) or len(rows) > PAGE_SIZE:
        raise ValueError("Mastercard refineSearch hit count does not reconcile")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Mastercard refineSearch row is not an object")
        job_id = row.get("jobId")
        sequence = row.get("jobSeqNo")
        id_match = JOB_ID.fullmatch(job_id or "")
        seq_match = JOB_SEQ.fullmatch(sequence or "")
        if (
            not id_match
            or not seq_match
            or id_match.group(1) != seq_match.group(1)
            or row.get("reqId") != job_id
            or not str(row.get("title") or "").strip()
            or row.get("siteType") != SITE_TYPE
            or row.get("visibilityType") != "External"
            or row.get("locale") != "en_US"
        ):
            raise ValueError(f"Mastercard refineSearch row has an inconsistent identity: {job_id!r}")
    return rows, value["totalHits"]


def parse_sitemap_files(index_text):
    """Files listed by the company sitemap index under its own base URL."""
    locs = re.findall(r"<loc>([^<]+)</loc>", index_text or "")
    files = [loc for loc in locs if loc.startswith(BASE_URL + "sitemap") and loc.endswith(".xml")]
    if not files:
        raise ValueError("Mastercard sitemap index publishes no job sitemap files")
    return files


def parse_sitemap_urls(xml_text, destination):
    """Add exact canonical job URLs to ``destination``; reject conflicts."""
    added = 0
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml_text or ""):
        if "/job/" not in loc:
            continue
        match = SITEMAP_JOB.search(loc)
        if not match:
            raise ValueError(f"Mastercard sitemap job URL has an unknown shape: {loc}")
        job_id = match.group(1)
        existing = destination.get(job_id)
        if existing is not None and existing != loc:
            raise ValueError(f"Mastercard sitemap repeats {job_id} with a different URL")
        if existing is None:
            added += 1
        destination[job_id] = loc
    return added


def parse_detail_response(payload, *, job_id, job_seq_no, title):
    """Validate one jobDetail response and return its job object."""
    value = payload.get("jobDetail") if isinstance(payload, dict) else None
    data = value.get("data", {}).get("job") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("status") != 200
        or value.get("hits") != 1
        or not isinstance(data, dict)
    ):
        raise ValueError(f"{job_id}: Mastercard jobDetail schema/status changed")
    if (
        data.get("jobId") != job_id
        or data.get("jobSeqNo") != job_seq_no
        or data.get("reqId") != job_id
        or data.get("refNum") != REFNUM
        or data.get("siteType") != SITE_TYPE
        or not str(data.get("companyName") or "").casefold().startswith("mastercard")
    ):
        raise ValueError(f"{job_id}: jobDetail identity does not match the company listing")
    if data.get("title") != title:
        raise ValueError(f"{job_id}: title conflict listing={title!r} detail={data.get('title')!r}")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{job_id}: full description field is missing")
    return data


def _locations(detail, listing):
    values = []
    multi = detail.get("multi_location")
    if isinstance(multi, list):
        for item in multi:
            location = item.get("location") if isinstance(item, dict) else None
            if isinstance(location, str) and location.strip() and location not in values:
                values.append(location.strip())
    if not values:
        for value in (
            detail.get("location"),
            listing.get("location"),
        ):
            if isinstance(value, str) and value.strip() and value not in values:
                values.append(value.strip())
    if not values:
        raw = listing.get("multi_location")
        if isinstance(raw, list):
            values = [value for value in raw if isinstance(value, str) and value.strip()]
    return values


class Mastercard(Adapter):
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
            import gzip

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
                            "public job fields; generated ML/embedding summaries, "
                            "chunk vectors, teasers and hashes omitted"
                        ),
                    }
                )
                + "\n"
            )

    def listing_body(self, offset):
        return {
            "lang": LOCALE,
            "deviceType": DEVICE,
            "country": COUNTRY,
            "pageName": PAGE_NAME,
            "ddoKey": "refineSearch",
            "from": offset,
            "size": PAGE_SIZE,
            "jobs": True,
            "counts": False,
            "global": True,
            "selected_fields": {},
            "all_fields": ["city", "state", "category", "type", "orgFunction", "country"],
            "clearAll": True,
            "keywords": "",
            "subsearch": "",
            "sortBy": "",
        }

    async def listing(self, offset):
        if not self.page_guard():
            raise ValueError("Mastercard listing page budget exhausted")
        body = self.listing_body(offset)
        response = await self.http.json(
            self.endpoint, method="POST", json=body, headers={"Referer": self.search_entry}
        )
        rows, total = parse_listing_response(response)
        return [_clean(row) for row in rows], total, body

    async def sitemap(self):
        index = await self.http.get(SITEMAP_INDEX)
        if str(index.url) != SITEMAP_INDEX:
            raise ValueError("Mastercard sitemap index redirected away from its published URL")
        files = parse_sitemap_files(index.text)
        urls = {}
        counts = []
        for file_url in files:
            page = await self.http.get(file_url)
            if str(page.url) != file_url:
                raise ValueError(f"Mastercard sitemap file redirected: {file_url}")
            locs = re.findall(r"<loc>([^<]+)</loc>", page.text)
            added = parse_sitemap_urls(page.text, urls)
            counts.append(
                {
                    "sitemap": file_url,
                    "locs": len(locs),
                    "job_urls": sum(1 for loc in locs if "/job/" in loc),
                    "new_job_ids": added,
                }
            )
        return urls, counts

    async def detail(self, job):
        listing = job.raw["listing"]
        body = {
            "lang": LOCALE,
            "deviceType": DEVICE,
            "country": COUNTRY,
            "pageName": "job",
            "ddoKey": "jobDetail",
            "jobSeqNo": listing["jobSeqNo"],
        }
        # httpx headers are ASCII only; the site's own listing URL is the
        # inspected Referer for the same-origin jobDetail widget request.
        payload = await self.http.json(
            self.endpoint, method="POST", json=body,
            headers={"Referer": self.search_entry},
        )
        data = parse_detail_response(
            payload,
            job_id=job.job_id,
            job_seq_no=listing["jobSeqNo"],
            title=job.title,
        )
        clean = _clean(data)
        self.evidence(self.endpoint, {"jobDetail": {"status": 200, "hits": 1, "totalHits": 1,
                                                    "data": {"job": clean}}}, body=body)
        description = data["description"]
        description_text = text_html(description)
        qualifications, responsibilities = sections_from_description(description_text)
        attachments = attachment_links(description)
        additional = data.get("additionalDescription")
        salary = salary_from_description(description_text)
        job.raw["detail"] = clean
        job.raw["native_detail_url"] = job.url
        job.raw["attachment_links"] = attachments
        job.raw["source_ui"] = {
            "listing_jobSeqNo": listing["jobSeqNo"],
            "detail_widget": self.endpoint,
            "detail_ddo_key": "jobDetail",
            "native_detail_url": job.url,
            "url_source": job.raw.get("url_source"),
            "rendered_apply_url": apply_route_url(listing["jobSeqNo"]),
            "ats": data.get("ats"),
            "ats_apply_url": data.get("applyUrl"),
            "action_type": data.get("actionType"),
            "external_apply": data.get("externalApply"),
            "job_type": data.get("jobType"),
            "is_evergreen": str(data.get("isEverGreenReq") or "").strip() == "1",
            "compensation_grade_id": data.get("compensationGradeId"),
            "compensation_grade_scope": data.get("compensationGradeScope"),
            "posted_date_track": data.get("postedDateTrack"),
            "job_reopened_on": data.get("jobReopenedOnDate"),
            "job_deleted_on": data.get("jobDeletedOnDate"),
            "posting_end_date": data.get("postingEndDate"),
            "salary_from_description": salary,
            "qualifications_excerpt": qualifications is not None,
            "responsibilities_excerpt": responsibilities is not None,
            "attachment_links": attachments,
            "additional_description_chars": len(additional) if isinstance(additional, str) else 0,
            "description_normalized_chars": len(normalised_body_text(description_text)),
            "description_fnv1a64": fnv1a64(normalised_body_text(description_text)),
        }
        job.description_html = description
        job.description_text = description_text
        job.description_method = "mastercard.native_widgets.jobDetail.description"
        job.description_source_url = self.endpoint
        job.title = data.get("title") or listing.get("title")
        job.requisition_id = data.get("jobRequisitionId") or data.get("reqId")
        job.apply_url = apply_route_url(listing["jobSeqNo"])
        job.locations = _locations(data, listing)
        job.department = data.get("category") or listing.get("category")
        job.team = data.get("jobFamilyGroup") or data.get("jobFamily") or None
        job.employment_type = data.get("type") or listing.get("type")
        job.workplace_type = data.get("remote")
        job.experience = None
        job.education = None
        job.date_posted = data.get("postedDate") or listing.get("postedDate")
        job.date_posted_raw = data.get("postedDate")
        job.date_updated = data.get("jobUpdatedDate")
        job.valid_through = data.get("postingEndDate") or None
        job.salary = salary
        job.employer = data.get("jobCompany") or data.get("companyName")
        job.language = data.get("locale")
        job.qualifications = qualifications
        job.responsibilities = responsibilities
        if job.raw["source_ui"]["is_evergreen"]:
            job.warnings.append("evergreen_pipeline_requisition")
        if attachments:
            job.warnings.append("role_attachment_links_present")
            self.result.errors.append(
                f"{job.job_id}: published role attachment links are present; "
                "attachment text was not captured"
            )
        if isinstance(additional, str) and additional.strip():
            self.result.errors.append(
                f"{job.job_id}: additionalDescription is non-empty and needs review"
            )
        return job

    async def _native_page(self, job):
        """Fetch one exact native job page and compare its embedded jobDetail."""
        response = await self.http.get(job.url)
        parsed = urlsplit(str(response.url))
        if parsed.hostname != "careers.mastercard.com" or "/job/" not in parsed.path:
            raise ValueError(f"{job.job_id}: native job page left its exact route: {response.url}")
        ddo = parse_page_ddo(response.text)
        value = ddo.get("jobDetail", {})
        data = value.get("data", {}).get("job") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or not isinstance(data, dict)
            or data.get("jobId") != job.job_id
            or data.get("jobSeqNo") != job.raw["listing"]["jobSeqNo"]
            or data.get("title") != job.title
        ):
            raise ValueError(f"{job.job_id}: native job page embedded jobDetail does not match")
        canonical = BeautifulSoup(response.text, "lxml").find("link", rel="canonical")
        canonical_url = canonical.get("href") if canonical else None
        canonical_slug = None
        if canonical_url:
            match = SITEMAP_JOB.search(unquote(canonical_url))
            canonical_slug = match.group(2) if match else None
        stored_slug = None
        match = SITEMAP_JOB.search(unquote(job.url))
        if match:
            stored_slug = match.group(2)
        body_matches = text_html(data.get("description") or "") == job.description_text
        return {
            "job_id": job.job_id,
            "native_url": job.url,
            "http_final_url": str(response.url),
            "canonical_url": canonical_url,
            "canonical_slug": canonical_slug,
            "stored_slug": stored_slug,
            "canonical_slug_matches_stored_job_url": canonical_slug is not None
            and canonical_slug == stored_slug,
            "embedded_jobdetail_matches_widget_description": body_matches,
            "embedded_description_normalized_chars": len(normalised_body_text(text_html(data.get("description") or ""))),
            "embedded_description_fnv1a64": fnv1a64(normalised_body_text(text_html(data.get("description") or ""))),
        }

    async def _paginate(self, *, label):
        """Union complete listing passes until every advertised jobId is seen."""
        rows_by_id = {}
        passes = 0
        total = None
        records = []
        duplicate_ids = set()
        first_pass_ids = []
        while True:
            passes += 1
            offset = 0
            ended_early = False
            while total is None or offset < total:
                rows, page_total, _body = await self.listing(offset)
                if total is None:
                    total = page_total
                elif page_total != total:
                    self.result.errors.append(
                        f"mastercard_total_changed_during_{label}_pass_{passes}"
                    )
                new_ids = [row["jobId"] for row in rows if row["jobId"] not in rows_by_id]
                repeated = [row["jobId"] for row in rows if row["jobId"] in rows_by_id]
                duplicate_ids.update(repeated)
                if passes == 1:
                    first_pass_ids.extend(row["jobId"] for row in rows)
                for row in rows:
                    rows_by_id.setdefault(row["jobId"], row)
                records.append(
                    {
                        "pass": passes,
                        "label": label,
                        "from": offset,
                        "hits": len(rows),
                        "totalHits": page_total,
                        "new_ids": len(new_ids),
                        "duplicate_ids": len(repeated),
                    }
                )
                if not rows:
                    self.result.errors.append(
                        f"mastercard_{label}_pass_{passes}_empty_page_before_total"
                    )
                    ended_early = True
                    break
                offset += len(rows)
            if total is not None and len(rows_by_id) >= total:
                break
            if ended_early:
                break
            if passes >= MAX_LISTING_PASSES:
                self.result.errors.append(
                    f"mastercard_{label}_union_stalled_at_{len(rows_by_id)}_of_{total}"
                )
                break
        return {
            "rows_by_id": rows_by_id,
            "total": total,
            "passes": passes,
            "records": records,
            "duplicate_ids": sorted(duplicate_ids),
            "first_pass_ids": first_pass_ids,
        }

    async def run(self):
        started = self.result._started_clock
        entry = await self.http.get(self.company.careers_url)
        if str(entry.url) != ENTRY:
            raise ValueError("Mastercard official search entry redirected away from its published URL")
        soup = BeautifulSoup(entry.text, "lxml")
        canonical = soup.find("link", rel="canonical")
        canonical_url = canonical.get("href") if canonical else None
        if canonical_url != ENTRY:
            raise ValueError("Mastercard official search entry no longer canonicalises to the listing")
        config = parse_app_config(entry.text)
        self.endpoint = config.get("widgetApiEndpoint")
        self.base = config.get("baseUrl", "")
        if (
            config.get("refNum") != REFNUM
            or self.endpoint != WIDGET_ENDPOINT
            or config.get("siteType") != SITE_TYPE
            or self.base != BASE_URL
            or config.get("locale") != LOCALE
            or config.get("country") != COUNTRY
            or config.get("deviceType") != DEVICE
            or config.get("pageName") != PAGE_NAME
        ):
            raise ValueError("Mastercard company/search configuration changed")
        self.search_entry = str(entry.url)
        eager_ids, eager_total = parse_eager_listing(entry.text)

        sitemap_started = time.perf_counter()
        sitemap_urls, sitemap_counts = await self.sitemap()
        sitemap_seconds = time.perf_counter() - sitemap_started

        listing_started = time.perf_counter()
        initial = await self._paginate(label="initial")
        listing_seconds = time.perf_counter() - listing_started
        rows_by_id = initial["rows_by_id"]
        total = initial["total"]
        passes = initial["passes"]
        page_records = initial["records"]
        self.result.expected_total = total
        self.result.pages = sum(1 for record in page_records if record["pass"] == 1)
        if not rows_by_id:
            self.result.errors.append(
                "Mastercard official listing returned zero rows; the empty state "
                "requires independent review"
            )
            return self.result
        if total is not None and eager_total != total:
            self.result.errors.append(
                f"mastercard_eager_total_{eager_total}_differs_from_widget_total_{total}"
            )
        if set(eager_ids) != set(initial["first_pass_ids"][: len(eager_ids)]):
            self.result.errors.append("mastercard_eager_ids_differ_from_widget_first_page")

        # Stale company-sitemap candidates: 410/404 -> gone, live -> unlisted error.
        sitemap_gone, sitemap_live_unlisted = [], []
        for job_id in sorted(set(sitemap_urls) - set(rows_by_id)):
            url = sitemap_urls[job_id]
            try:
                response = await self.http.get(url)
                sitemap_live_unlisted.append({"jobId": job_id, "url": url, "status": response.status_code})
                self.result.errors.append(
                    f"{job_id}: live company-sitemap candidate is missing from the listing"
                )
            except Exception as exc:  # FetchError on 404/410
                text = str(exc)
                if "HTTP 410" in text or "HTTP 404" in text:
                    sitemap_gone.append({"jobId": job_id, "url": url, "reason": text})
                else:
                    sitemap_live_unlisted.append({"jobId": job_id, "url": url, "error": text})
                    self.result.errors.append(
                        f"{job_id}: stale-candidate probe failed inconclusively: {text}"
                    )
        missing_from_sitemap = sorted(set(rows_by_id) - set(sitemap_urls))

        for job_id, row in rows_by_id.items():
            url_source = "company_sitemap_canonical_url" if job_id in sitemap_urls else "title_slug_route"
            url = sitemap_urls.get(job_id) or native_job_url(job_id, row["title"])
            job = self.job(
                {"listing": row, "url_source": url_source},
                title=row["title"],
                job_id=job_id,
                requisition_id=job_id,
                url=url,
                date_posted=row.get("postedDate"),
                date_posted_raw=row.get("postedDate"),
                apply_url=apply_route_url(row["jobSeqNo"]),
            )
            self.add(job)
        if "sample_limit" in self.result.warnings:
            self.result.listing_complete = False
            self.result.metadata["company_published_job_ids"] = sorted(rows_by_id)
            return self.result

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        page_checks = []
        representatives = list(self.result.jobs[:1]) + list(self.result.jobs[-1:])
        slug_unverified = [job for job in self.result.jobs
                           if job.raw.get("url_source") == "title_slug_route"
                           and job.job_id not in {item.job_id for item in representatives}]
        representatives += slug_unverified[:3]
        for job in representatives:
            try:
                check = await self._native_page(job)
                page_checks.append(check)
                if not check["embedded_jobdetail_matches_widget_description"]:
                    self.result.errors.append(
                        f"{job.job_id}: native page description differs from jobDetail widget"
                    )
                if not check["canonical_slug_matches_stored_job_url"]:
                    self.result.errors.append(
                        f"{job.job_id}: native canonical slug differs from the stored job URL"
                    )
            except Exception as exc:
                page_checks.append(
                    {
                        "job_id": job.job_id,
                        "native_url": job.url,
                        "embedded_jobdetail_matches_widget_description": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                self.result.errors.append(
                    f"{job.job_id}: representative native page failed: {type(exc).__name__}: {exc}"
                )

        final_started = time.perf_counter()
        final_sets = []
        final_ids = set()
        for attempt in range(1, MAX_FINAL_PROOF_ATTEMPTS + 1):
            ids = set()
            pass_total = None
            offset = 0
            while pass_total is None or offset < pass_total:
                rows, page_total, _body = await self.listing(offset)
                pass_total = page_total
                if page_total != total:
                    self.result.errors.append("mastercard_total_changed_before_final_proof")
                ids.update(row["jobId"] for row in rows)
                offset += len(rows)
                if not rows:
                    break
            final_sets.append({"attempt": attempt, "ids": len(ids), "total": pass_total,
                               "matches_union": ids == set(rows_by_id)})
            final_ids = ids
            if ids == set(rows_by_id):
                break
        final_seconds = max(0.0, time.perf_counter() - final_started)

        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry_url=str(entry.url),
            official_entry_status=entry.status_code,
            official_entry_canonical=canonical_url,
            native_search_entry=self.search_entry,
            listing_authority=(
                "Mastercard unfiltered global search on its own Phenom site "
                "(careers.mastercard.com refineSearch, clearAll=true, global=true, "
                "no keyword/location/category filters); the UI renders the same "
                "membership 5 rows per page over 191 pages"
            ),
            widget_api_endpoint=self.endpoint,
            refNum=REFNUM,
            page_size=PAGE_SIZE,
            ui_rows_per_page=5,
            ui_total_pages=-(total // -5) if total else None,
            eager_load_total_hits=eager_total,
            eager_load_job_ids=eager_ids,
            widget_total_hits=total,
            listing_passes=passes,
            listing_pages=page_records,
            listing_union_ids=len(rows_by_id),
            listing_order_ids=list(rows_by_id),
            duplicate_ids_across_passes=initial["duplicate_ids"],
            sitemap_index_url=SITEMAP_INDEX,
            sitemap_files=sitemap_counts,
            sitemap_candidate_count=len(sitemap_urls),
            sitemap_gone_candidates=sitemap_gone,
            live_sitemap_but_unlisted=sitemap_live_unlisted,
            listed_ids_absent_from_sitemap=missing_from_sitemap,
            sitemap_note=(
                "The company sitemap is an SEO artifact, not the membership authority: "
                "it supplies exact canonical job URLs and stale-candidate detection. "
                f"{len(missing_from_sitemap)} listed job(s) absent from the current sitemap; "
                f"{len(sitemap_gone)} sitemap-only candidate(s) returned 404/410 Gone and "
                "were recorded as stale; "
                f"{len(sitemap_live_unlisted)} live sitemap-only candidate(s) remained "
                "unlisted. Membership comes from the company search listing."
            ),
            representative_native_pages=page_checks,
            representative_native_page_count=len(page_checks),
            detail_request_count=sum(1 for job in self.result.jobs if job.raw.get("detail")),
            final_proof_sets=final_sets,
            final_proof_ids=len(final_ids),
            country_count=len({row.get("country") for row in rows_by_id.values()}),
            category_count=len({row.get("category") for row in rows_by_id.values()}),
            part_time_count=sum(1 for row in rows_by_id.values() if row.get("type") == "Part time"),
            evergreen_job_ids=sorted(
                job.job_id for job in self.result.jobs
                if (job.raw.get("source_ui") or {}).get("is_evergreen")
            ),
            salary_from_description_ids=sorted(
                job.job_id for job in self.result.jobs if job.raw.get("source_ui", {}).get("salary_from_description")
            ),
            attachment_link_job_ids=sorted(
                job.job_id for job in self.result.jobs if job.raw.get("attachment_links")
            ),
            additional_description_job_ids=sorted(
                job.job_id for job in self.result.jobs
                if (job.raw.get("source_ui") or {}).get("additional_description_chars")
            ),
            description_source="Mastercard jobDetail widget description (same object the native job page embeds and renders)",
            date_policy=(
                "postedDate is the source posting date; jobUpdatedDate is the update "
                "date; postingEndDate (shown as End Date) is valid_through. dateCreated, "
                "postedDateTrack, jobReopenedOnDate and jobDeletedOnDate are pipeline "
                "history and stay only in raw."
            ),
            collection_wall_seconds=round(time.perf_counter() - started, 6),
        )
        self.result.timings["sitemap_phase_seconds"] = round(sitemap_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_wall_seconds"] = round(detail_seconds, 6)
        self.result.timings["final_proof_phase_seconds"] = round(final_seconds, 6)
        self.result.metadata["company_published_job_ids"] = sorted(rows_by_id)
        self.result.listing_complete = (
            not self.result.errors
            and total is not None
            and len(rows_by_id) == total
            and final_ids == set(rows_by_id)
            and "sample_limit" not in self.result.warnings
        )
        return self.result
