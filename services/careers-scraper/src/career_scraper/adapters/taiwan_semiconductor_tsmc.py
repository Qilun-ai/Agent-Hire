"""TSMC's own Avature careers portal: the 805-row search listing and exact JobDetail pages.

Observed journey (2026-11-08 UTC):
- ``https://careers.tsmc.com/`` 302-redirects to ``https://careers.tsmc.com/en_US/careers``,
  which is itself the job search listing (``Jobs | TSMC Careers``).
- The listing reports ``1-10 of 805 results`` and paginates by
  ``/en_US/careers/SearchJobs/?jobRecordsPerPage=10&jobOffset=N`` (81 pages, last page 5 rows).
- The default listing order is not stable between requests.  The published UI sort control
  ``Title (A - Z)`` (``jobSort=jobname&jobSortDirection=asc``) is deterministic and is used for
  pagination, so every offset is collected exactly once.
- Every row links to an exact ``/en_US/careers/JobDetail?jobId=<id>&source=...`` page.  Detail
  pages expose ``Company`` (legal employer), ``Location``, optional ``Career Area``/``Job Type``,
  ``Employment Type`` and ``Posted`` plus the employer-authored body sections
  (``Job Responsibilities``, ``Job Qualifications``, ``TSMC/JASM Core Values`` and unlabelled
  corporate/role blocks).  Apply is ``/en_US/careers/Login?jobId=<id>``.
- ``/en_US/careers/sitemap.xml`` publishes the same 805 JobDetail IDs and is used only as an
  independent completeness cross-check; listing membership is the authority.

No facet, keyword or location filter is applied.  No aggregator, guessed ATS slug or parent
board is used: this is TSMC's own portal, listing TSMC Group / ESMC / JASM legal entities.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://careers.tsmc.com/"
PORTAL_HOST = "careers.tsmc.com"
PORTAL_HOME = "https://careers.tsmc.com/en_US/careers"
LISTING_URL = "https://careers.tsmc.com/en_US/careers/SearchJobs/"
SITEMAP_URL = "https://careers.tsmc.com/en_US/careers/sitemap.xml"
DETAIL_PATH = "/en_US/careers/JobDetail"
APPLY_PATH = "/en_US/careers/Login"
PAGE_SIZE = 10
SORT_QUERY = "jobSort=jobname&jobSortDirection=asc"
# The public pages are served to an ordinary browser UA; the same UA is used here and a
# Cloudflare challenge page (if ever served) is treated as a hard failure, never solved.
REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
LEGEND = re.compile(r"(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+results")
POSTED = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4})$")
JOB_ID = re.compile(r"^\d+$")
CURRENCY_AMOUNT = re.compile(
    r"(NTD|TWD|USD|EUR|JPY|CNY|RMB|CAD|KRW|GBP)\s*\$?\s*([\d,]+(?:\.\d+)?)", re.I)
SALARY_HINT = re.compile(
    r"薪資|月薪|年薪|時薪|日薪|待遇|salary|compensation|pay range|hourly|per hour|per year",
    re.I)
SALARY_PERIODS = (
    ("month", re.compile(r"月薪|per month|monthly|/month", re.I)),
    ("year", re.compile(r"年薪|per year|annual|/year", re.I)),
    ("hour", re.compile(r"時薪|per hour|hourly|/hour", re.I)),
    ("day", re.compile(r"日薪|per day|daily|/day", re.I)),
)
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


class ChallengePage(ValueError):
    """The portal returned a bot-protection interstitial instead of the public page."""


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None, *, is_html: bool = True) -> str:
    """NFC, whitespace-collapsed visible text used for independent rendered comparisons."""
    text = text_html(value or "") if is_html else (value or "")
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def listing_page_url(offset: int) -> str:
    if offset < 0 or offset % PAGE_SIZE:
        raise ValueError(f"TSMC listing offset must be a non-negative multiple of {PAGE_SIZE}")
    return f"{LISTING_URL}?{SORT_QUERY}&jobRecordsPerPage={PAGE_SIZE}&jobOffset={offset}"


def _check_portal_url(url: str, *, path: str) -> None:
    parts = urlsplit(str(url))
    if parts.scheme != "https" or parts.netloc != PORTAL_HOST or parts.path != path:
        raise ValueError(f"TSMC flow left its official portal route: {url}")


def _challenge(html: str) -> bool:
    probe = (html or "")[:4000].lower()
    return ("just a moment" in probe and "challenge" in probe) or "cf_chl_opt" in probe


def _direct_field(article):
    """The article's own field wrapper, ignoring fields nested in child articles."""
    content = article.find("div", class_="article__content", recursive=False)
    view = content.find("div", class_="article__content__view", recursive=False) if content else None
    return view.find("div", class_="article__content__view__field", recursive=False) if view else None


def _label_and_value(article):
    field = _direct_field(article)
    if field is None:
        return None, None
    label = field.find("div", class_="article__content__view__field__label", recursive=False)
    value = field.find("div", class_="article__content__view__field__value", recursive=False)
    return label, value


def parse_official_entry(html: str, response_url: str = PORTAL_HOME) -> dict:
    _check_portal_url(response_url, path="/en_US/careers")
    if _challenge(html):
        raise ChallengePage("TSMC careers entry returned a Cloudflare challenge page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Jobs | TSMC Careers":
        raise ValueError(f"TSMC careers entry title changed: {title!r}")
    legend = LEGEND.search(soup.get_text(" ", strip=True))
    if legend is None:
        raise ValueError("TSMC careers entry no longer reports a results legend")
    rows = soup.select("article.article--result")
    pagination = [a.get_text(" ", strip=True) for a in soup.select("a[href*='jobOffset']")]
    listing_links = [a.get("href") for a in soup.select("a[href]")
                     if urlsplit(a.get("href", "")).path == "/en_US/careers/SearchJobs"]
    return {
        "url": response_url,
        "title": title,
        "results_total": int(legend.group(3)),
        "results_range": [int(legend.group(1)), int(legend.group(2))],
        "page_rows": len(rows),
        "pagination_labels": pagination,
        "listing_links": listing_links,
        "language": soup.html.get("lang") if soup.html else None,
    }


def parse_listing_page(html: str, response_url: str, expected_offset: int | None = None) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != PORTAL_HOST or parts.path.rstrip("/") != "/en_US/careers/SearchJobs":
        raise ValueError(f"TSMC listing response came from an unexpected route: {response_url}")
    query = parse_qs(parts.query)
    offset = int(query.get("jobOffset", ["0"])[0])
    if expected_offset is not None and offset != expected_offset:
        raise ValueError(f"TSMC listing page returned offset {offset}, expected {expected_offset}")
    if query.get("jobSort", [None])[0] != "jobname" or query.get("jobSortDirection", [None])[0] != "asc":
        raise ValueError("TSMC listing page did not preserve the reviewed Title (A - Z) sort")
    if _challenge(html):
        raise ChallengePage(f"TSMC listing offset {offset} returned a Cloudflare challenge page")
    soup = BeautifulSoup(html, "lxml")
    if normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "") != "Jobs | TSMC Careers":
        raise ValueError(f"TSMC listing offset {offset} title changed")
    legend = LEGEND.search(soup.get_text(" ", strip=True))
    if legend is None:
        raise ValueError(f"TSMC listing offset {offset} has no results legend")
    start, end, total = (int(value) for value in legend.groups())
    if start != offset + 1 or end != min(offset + PAGE_SIZE, total):
        raise ValueError(f"TSMC listing offset {offset} legend {start}-{end} of {total} is inconsistent")
    rows, issues, seen = [], [], set()
    for article in soup.select("article.article--result"):
        anchor = article.select_one("h3 a[href]")
        if anchor is None:
            issues.append("listing_card_without_title_link")
            continue
        href = anchor.get("href", "")
        link = urlsplit(href)
        link_query = parse_qs(link.query)
        job_id = (link_query.get("jobId") or [None])[0]
        if (link.scheme != "https" or link.netloc != PORTAL_HOST or link.path != DETAIL_PATH
                or not job_id or not JOB_ID.fullmatch(job_id)):
            issues.append(f"listing_card_non_exact_detail_link:{href}")
            continue
        if job_id in seen:
            issues.append(f"listing_card_duplicate_job_id:{job_id}")
            continue
        seen.add(job_id)
        location = article.select_one(".list-item-location")
        employment = article.select_one(".list-item-employmentType")
        posted = article.select_one(".list-item-posted")
        posted_label = normalized_title(posted.get_text(" ", strip=True)) if posted else None
        posted_value = re.sub(r"^Posted:\s*", "", posted_label or "", flags=re.I) or None
        posted_date = None
        if posted_value and POSTED.fullmatch(posted_value):
            posted_date = datetime.strptime(posted_value, "%b %d, %Y").date().isoformat()
        elif posted_value:
            issues.append(f"listing_card_posted_date_unparsed:{job_id}:{posted_value}")
        rows.append({
            "job_id": job_id,
            "title": normalized_title(anchor.get_text(" ", strip=True)),
            "url": href,
            "listing_location": normalized_title(location.get_text(" ", strip=True)) if location else None,
            "listing_employment_type": normalized_title(employment.get_text(" ", strip=True)) if employment else None,
            "posted_label": posted_label,
            "posted_date": posted_date,
            "listing_offset": offset,
        })
    expected_rows = end - start + 1
    if len(rows) != expected_rows:
        issues.append(f"listing_card_count_mismatch:{len(rows)}!={expected_rows}")
    next_link = next((a.get("href") for a in soup.select("a[href*='jobOffset']")
                      if normalized_title(a.get_text(" ", strip=True)).lower().startswith("next")), None)
    return {
        "url": response_url,
        "offset": offset,
        "range": [start, end],
        "total": total,
        "rows": rows,
        "ids": [row["job_id"] for row in rows],
        "next_url": next_link,
        "issues": issues,
    }


def parse_sitemap_job_ids(xml: str, response_url: str = SITEMAP_URL) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != PORTAL_HOST or parts.path != "/en_US/careers/sitemap.xml":
        raise ValueError(f"TSMC sitemap response came from an unexpected route: {response_url}")
    if _challenge(xml):
        raise ChallengePage("TSMC sitemap returned a Cloudflare challenge page")
    locs = re.findall(r"<loc>([^<]+)</loc>", xml or "")
    if not locs:
        raise ValueError("TSMC sitemap response contains no <loc> entries")
    urls_by_id, issues = {}, []
    for loc in locs:
        if "/careers/JobDetail" not in loc:
            continue
        match = re.search(r"/(\d+)/?$", loc)
        if match is None:
            # Portal pages such as /en_US/careers/JobDetail and /JobDetailSuccessFactors are
            # linked from the same sitemap but are not job postings.
            continue
        identity = match.group(1)
        if identity in urls_by_id:
            issues.append(f"sitemap_duplicate_job_id:{identity}")
            continue
        urls_by_id[identity] = loc
    return {"url": response_url, "loc_count": len(locs), "urls_by_id": urls_by_id,
            "ids": list(urls_by_id), "issues": issues}


def parse_posted_date(value: str | None) -> tuple[str | None, str | None]:
    raw = normalized_title(value)
    if not raw:
        return None, None
    if not POSTED.fullmatch(raw):
        return None, raw
    return datetime.strptime(raw, "%b %d, %Y").date().isoformat(), raw


def description_sections(main, field_article=None) -> tuple[list[dict], list[str]]:
    """All non-empty employer-authored body blocks in DOM order, with their labels."""
    issues, sections, seen = [], [], set()
    for article in main.select("article.article--details"):
        if field_article is not None and article is field_article:
            continue
        label_node, value_node = _label_and_value(article)
        if value_node is None:
            continue
        html = value_node.decode_contents()
        raw_text = text_html(html)
        text = normalized_body_text(raw_text, is_html=False)
        if not text:
            continue
        label = normalized_title(label_node.get_text(" ", strip=True)) if label_node else None
        key = (label, text)
        if key in seen:
            issues.append(f"duplicate_body_section_skipped:{label or 'unlabelled'}")
            continue
        seen.add(key)
        sections.append({
            "label": label,
            "html": html,
            "raw_text": raw_text,
            "text": text,
            "chars": len(text),
            "fnv1a64": fnv1a64(text),
        })
    return sections, issues


RESPONSIBILITY_HEADING = re.compile(
    r"(?:Key Responsibilities|Job Responsibilities|Job Description|Responsibilities)\s*:", re.I)
QUALIFICATION_HEADING = re.compile(
    r"(?:Required Qualifications|Preferred Qualifications|Job Qualifications|Qualifications|Requirements)\s*:",
    re.I)
QUALIFICATION_END = re.compile(
    r"(?:Working Conditions|Selection Process|Employee Benefits|About (?:the )?(?:Role|Team|Company)|What We Offer)\s*:",
    re.I)


def _inline_role_split(sections: list[dict]) -> tuple[str | None, str | None]:
    """Split an unlabelled block that carries its own responsibility/qualification headings.

    The full block always stays in the description; this only fills the structured fields when
    both headings are explicitly published.
    """
    for section in sections:
        if section["label"]:
            continue
        text = section["raw_text"]
        resp = RESPONSIBILITY_HEADING.search(text)
        qual = QUALIFICATION_HEADING.search(text, resp.end() if resp else 0)
        if resp is None or qual is None:
            continue
        end = QUALIFICATION_END.search(text, qual.end())
        responsibilities = text[resp.end():qual.start()].strip() or None
        qualifications = text[qual.start():end.start() if end else len(text)].strip() or None
        if responsibilities and qualifications:
            return responsibilities, qualifications
    return None, None


def _section_text(sections: list[dict], pattern: str) -> str | None:
    values = [section["text"] for section in sections
              if section["label"] and re.search(pattern, section["label"], re.I)]
    return "\n\n".join(values) if values else None


def salary_from_description(sections: list[dict]) -> dict | None:
    """Retain only explicit salary/compensation lines; never infer a range."""
    found, seen = [], set()
    for section in sections:
        for line in (section.get("raw_text") or "").splitlines():
            value = line.strip()
            if not value or not SALARY_HINT.search(value):
                continue
            amounts = CURRENCY_AMOUNT.findall(value)
            if not amounts:
                continue
            for currency, amount in amounts:
                period = next((name for name, pattern in SALARY_PERIODS if pattern.search(value)), None)
                key = (value, currency.upper(), amount, period)
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "source_text": value,
                    "source_section": section["label"],
                    "currency": currency.upper(),
                    "amount": float(amount.replace(",", "")),
                    "period": period,
                })
    return found or None


def parse_detail_page(html: str, response_url: str, expected_row: dict) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != PORTAL_HOST or parts.path != DETAIL_PATH:
        raise ValueError(f"TSMC detail response came from an unexpected route: {response_url}")
    query = parse_qs(parts.query)
    job_id = (query.get("jobId") or [None])[0]
    if not job_id or not JOB_ID.fullmatch(job_id):
        raise ValueError(f"TSMC detail URL has no numeric jobId: {response_url}")
    if job_id != str(expected_row["job_id"]):
        raise ValueError(f"TSMC detail request redirected to a different job: {response_url}")
    if _challenge(html):
        raise ChallengePage(f"TSMC JobDetail {job_id} returned a Cloudflare challenge page")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h2.banner__text__title")
    title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        raise ValueError(f"TSMC JobDetail {job_id} has no banner title")
    issues = []
    warnings = []
    if title != normalized_title(expected_row.get("title")):
        issues.append(f"detail_listing_title_mismatch:{job_id}")
    main = soup.select_one("div.grid__item--main")
    if main is None:
        raise ValueError(f"TSMC JobDetail {job_id} has no main content column")

    fields, field_article = {}, None
    articles = main.select("article.article--details")
    if articles:
        field_article = articles[0]
        content = field_article.find("div", class_="article__content", recursive=False)
        view = content.find("div", class_="article__content__view", recursive=False) if content else None
        if view is not None:
            for field in view.find_all("div", class_="article__content__view__field", recursive=False):
                label = field.find("div", class_="article__content__view__field__label", recursive=False)
                value = field.find("div", class_="article__content__view__field__value", recursive=False)
                if label is None or value is None:
                    continue
                fields[normalized_title(label.get_text(" ", strip=True))] = normalized_title(
                    value.get_text(" ", strip=True))
    if "Company" not in fields:
        raise ValueError(f"TSMC JobDetail {job_id} has no Company field")
    if "Location" not in fields:
        raise ValueError(f"TSMC JobDetail {job_id} has no Location field")
    if "Posted" not in fields:
        # A posting may genuinely publish no date; it stays null and is recorded, not fabricated.
        warnings.append(f"detail_posted_field_missing:{job_id}")

    sections, section_issues = description_sections(main, field_article)
    issues.extend(section_issues)
    field_labels = {"Company", "Location", "Career Area", "Job Type", "Employment Type", "Posted"}
    if any(section["label"] in field_labels for section in sections):
        issues.append(f"detail_fields_leaked_into_body:{job_id}")
    if not sections:
        issues.append(f"detail_body_empty:{job_id}")

    chunks = []
    for section in sections:
        if section["label"]:
            chunks.append(
                f'<section data-tsmc-section="{escape(section["label"])}">'
                f"<h3>{escape(section['label'])}</h3>{section['html']}</section>")
        else:
            chunks.append(f'<section data-tsmc-section="body">{section["html"]}</section>')
    description_html = "".join(chunks)
    description_text = text_html(description_html)
    normalized = normalized_body_text(description_text, is_html=False)

    responsibilities = _section_text(sections, r"responsibilit")
    qualifications = _section_text(sections, r"qualif")
    inline_responsibilities, inline_qualifications = _inline_role_split(sections)
    responsibilities = responsibilities or inline_responsibilities
    qualifications = qualifications or inline_qualifications

    posted_date, posted_raw = parse_posted_date(fields.get("Posted"))
    apply_url, apply_route = None, None
    apply_node = soup.select_one("article.article--actions a.button--primary[href]")
    if apply_node is not None:
        apply_parts = urlsplit(apply_node["href"])
        apply_query = parse_qs(apply_parts.query)
        if (apply_parts.scheme == "https" and apply_parts.netloc == PORTAL_HOST
                and apply_parts.path in ("/en_US/careers/Login", "/en_US/careers/JobDetailSuccessFactors")
                and (apply_query.get("jobId") or [None])[0] == job_id):
            apply_url = apply_node["href"]
            apply_route = ("portal_login" if apply_parts.path == "/en_US/careers/Login"
                           else "successfactors_handoff")
        else:
            issues.append(f"detail_apply_link_unexpected:{job_id}:{apply_node['href']}")
    else:
        issues.append(f"detail_apply_link_missing:{job_id}")

    canonical_url = None
    share = soup.select_one("a.shareButton--email[href]")
    if share is not None:
        match = re.search(r"[?&]body=([^&]+)", share["href"])
        if match:
            candidate = unquote(match.group(1))
            candidate_parts = urlsplit(candidate)
            if (candidate_parts.scheme == "https" and candidate_parts.netloc == PORTAL_HOST
                    and candidate_parts.path.startswith("/en_US/careers/JobDetail/")):
                canonical_url = candidate

    jsonld = None
    script = soup.select_one('script[type="application/ld+json"]')
    if script is not None and script.string:
        try:
            value = json.loads(script.string)
        except ValueError:
            issues.append(f"detail_jsonld_invalid:{job_id}")
            value = None
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            identifier = value.get("identifier") or {}
            if str(identifier.get("value")) != job_id:
                issues.append(f"detail_jsonld_identifier_mismatch:{job_id}")
            jsonld = {
                "datePosted": value.get("datePosted"),
                "identifier": identifier,
                "employmentType": value.get("employmentType"),
                "hiringOrganization": value.get("hiringOrganization"),
                "jobLocation": value.get("jobLocation"),
                "title": value.get("title"),
                "responsibilities_html": value.get("responsibilities"),
                "qualifications_html": value.get("qualifications"),
            }

    links = []
    role_attachment_links = []
    for section in sections:
        role_section = bool(section["label"] and re.search(r"responsibilit|qualif", section["label"], re.I))
        if not section["label"] and section is not None:
            role_section = bool(RESPONSIBILITY_HEADING.search(section["raw_text"] or ""))
        for anchor in BeautifulSoup(section["html"], "lxml").select("a[href]"):
            href = anchor.get("href", "").strip()
            link = {"url": href, "label": normalized_title(anchor.get_text(" ", strip=True)),
                    "attachment": bool(ATTACHMENT.search(href)),
                    "section": section["label"], "role_section": role_section}
            links.append(link)
            if link["attachment"] and role_section:
                role_attachment_links.append(link)
    if role_attachment_links:
        issues.append(f"detail_role_attachments_require_review:{job_id}")

    salary = salary_from_description(sections)
    return {
        "job_id": job_id,
        "url": response_url,
        "title": title,
        "fields": fields,
        "sections": sections,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized),
        "description_fnv1a64": fnv1a64(normalized),
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "salary": salary,
        "apply_url": apply_url,
        "apply_route": apply_route,
        "canonical_url": canonical_url,
        "jsonld": jsonld,
        "links": links,
        "issues": issues,
        "warnings": warnings,
    }


class TaiwanSemiconductorTsmc(Adapter):
    """Collect the exact 805-row TSMC portal listing and its exact JobDetail pages."""

    VERIFICATION_REVISION = "tsmc.avature-careers-listing-and-jobdetail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.blocked_reason = None

    async def _get(self, url: str):
        return await self.http.get(url, headers=REQUEST_HEADERS)

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self._get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        _check_portal_url(str(response.url), path="/en_US/careers")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _sitemap(self):
        started = time.perf_counter()
        attempts = []
        last_error = None
        for accept in (None, "text/xml,application/xml;q=0.9,*/*;q=0.8", "*/*"):
            headers = {"User-Agent": REQUEST_HEADERS["User-Agent"],
                       "Accept-Language": REQUEST_HEADERS["Accept-Language"]}
            if accept:
                headers["Accept"] = accept
            response = await self._get_with(SITEMAP_URL, headers)
            attempts.append({"accept": accept or "client-default", "status": response.status_code,
                             "bytes": len(response.content)})
            if response.content:
                parsed = parse_sitemap_job_ids(response.text, str(response.url))
                parsed.update(status=response.status_code, seconds=max(0.0, time.perf_counter() - started),
                              response_sha256=sha256(response.content).hexdigest(),
                              response_bytes=len(response.content), attempts=attempts)
                return parsed
            last_error = f"empty sitemap response ({len(response.content)} bytes) with Accept {accept!r}"
        raise ValueError(last_error or "empty sitemap response")

    async def _get_with(self, url: str, headers: dict):
        return await self.http.get(url, headers=headers)

    async def _listing_page(self, offset: int):
        started = time.perf_counter()
        response = await self._get(listing_page_url(offset))
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_page(response.text, str(response.url), expected_offset=offset)
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _listing_sweep(self, label: str):
        started = time.perf_counter()
        first = await self._listing_page(0)
        total = first["total"]
        pages, rows = [first], list(first["rows"])
        for offset in range(PAGE_SIZE, total, PAGE_SIZE):
            if not self.page_guard():
                raise ValueError(f"TSMC {label} page budget exhausted at offset {offset}")
            page = await self._listing_page(offset)
            if page["total"] != total:
                raise ValueError(f"TSMC {label} listing total changed: {total} -> {page['total']}")
            pages.append(page)
            rows.extend(page["rows"])
        ids = [row["job_id"] for row in rows]
        issues = [issue for page in pages for issue in page["issues"]]
        if len(ids) != len(set(ids)):
            issues.append(f"{label}_listing_repeats_job_ids")
        if len(ids) != total:
            issues.append(f"{label}_listing_unique_count_mismatch:{len(set(ids))}!={total}")
        return {
            "label": label,
            "total": total,
            "pages": [{key: page[key] for key in
                       ("url", "offset", "range", "total", "ids", "issues", "status",
                        "seconds", "response_sha256", "response_bytes")} for page in pages],
            "rows": rows,
            "ids": ids,
            "issues": issues,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _detail(self, job):
        row = job.raw["listing"]
        if self.blocked_reason:
            return self._unavailable_job(job, f"skipped_after_source_block: {self.blocked_reason}")
        try:
            response = await self._get(row["url"])
            parsed = parse_detail_page(response.text, str(response.url), row)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if "HTTP 429" in reason or "HTTP 403" in reason or isinstance(exc, ChallengePage):
                self.blocked_reason = reason
            self.result.errors.append(f"TSMC JobDetail {row['job_id']} ({row['url']}): {reason}")
            return self._unavailable_job(job, reason)
        for issue in parsed["issues"]:
            self.result.errors.append(f"TSMC JobDetail {row['job_id']}: {issue}")
        for warning in parsed.get("warnings", []):
            job.warnings.append(warning)
        return self._make_job(row, parsed, response)

    def _unavailable_job(self, job, reason):
        job.description_unavailable = True
        job.warnings.append("detail_fetch_failed")
        job.raw["detail_error"] = reason
        return job

    def _make_job(self, row, parsed, response):
        fields = parsed["fields"]
        locations = [fields["Location"]] if fields.get("Location") else (
            [row["listing_location"]] if row.get("listing_location") else [])
        raw = {
            "listing": row,
            "detail_fields": fields,
            "sections": parsed["sections"],
            "jsonld": parsed["jsonld"],
            "links": parsed["links"],
            "canonical_url": parsed["canonical_url"],
            "apply_url": parsed["apply_url"],
            "apply_route": parsed["apply_route"],
            "detail_response_sha256": sha256(response.content).hexdigest(),
            "detail_response_bytes": len(response.content),
            "posted_date_mapping": {
                "source_label": "Posted",
                "raw": parsed["fields"].get("Posted"),
                "ui_sort_label": "Latest Update",
                "note": ("Visible 'Posted' field retained; the portal sort menu calls the same "
                         "field 'Latest Update'. RSS pubDate is a different, older value and is "
                         "not used as a posting date."),
            },
        }
        job = self.job(
            raw,
            title=parsed["title"],
            url=row["url"],
            job_id=row["job_id"],
            requisition_id=None,
            apply_url=parsed["apply_url"],
            description=parsed["description_html"],
            method="tsmc.avature.jobdetail.article_details_sections",
            description_url=row["url"],
            locations=locations,
            department=fields.get("Career Area"),
            team=fields.get("Job Type"),
            employment_type=fields.get("Employment Type") or row.get("listing_employment_type"),
            workplace_type=None,
            date_posted=parsed["fields"].get("Posted") and self._posted_date(parsed["fields"]["Posted"]),
            date_posted_raw=parsed["fields"].get("Posted"),
            date_updated=None,
            valid_through=None,
            salary=parsed["salary"],
            qualifications=parsed["qualifications"],
            responsibilities=parsed["responsibilities"],
            employer=fields.get("Company"),
            language="en",
        )
        job.description_text = parsed["description_text"]
        job.record_identity = f"tsmc:{row['job_id']}"
        normalized = normalized_body_text(job.description_text, is_html=False)
        job.raw["detail"] = {
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": [
                {key: section[key] for key in ("label", "chars", "fnv1a64")}
                for section in parsed["sections"]
            ],
            "salary_from_description": parsed["salary"],
            "role_attachments": [link for link in parsed["links"]
                                 if link["attachment"] and link["role_section"]],
            "corporate_links": [link for link in parsed["links"] if not link["role_section"]],
        }
        return job

    @staticmethod
    def _posted_date(value: str | None):
        parsed, _ = parse_posted_date(value)
        return parsed

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "portal_home": PORTAL_HOME,
            "listing_url": LISTING_URL,
            "listing_authority": ("TSMC's own Avature careers portal search listing "
                                  "(https://careers.tsmc.com/en_US/careers/SearchJobs/)."),
            "listing_request_shape": {
                "method": "GET",
                "url_template": listing_page_url(0),
                "page_size": PAGE_SIZE,
                "sort": {"jobSort": "jobname", "jobSortDirection": "asc",
                         "reason": "Published UI sort control; the default order is not stable between requests."},
                "filters_applied": False,
                "facet_controls_not_applied": ["Organization", "Location", "Job Category",
                                                "Job Type", "Employment Type"],
            },
            "detail_method": ("Each exact listing JobDetail href is fetched; the employer body is every "
                              "non-empty article--details block in the main column, in DOM order."),
            "apply_route": ("Detail Apply button points to /en_US/careers/Login?jobId=<id> "
                            "(registration/login required); no separate form action is published."),
            "date_mapping": ("Only the visible 'Posted' field is used as date_posted; RSS pubDate and "
                             "JSON-LD datePosted (null) are not posting dates."),
            "request_user_agent": REQUEST_HEADERS["User-Agent"],
            "bot_protection_policy": ("A Cloudflare challenge page or HTTP 403/429 is a hard failure; "
                                      "no CAPTCHA is solved and no challenge is bypassed."),
        })
        try:
            entry = await self._official_entry()
        except Exception as exc:
            self.result.errors.append(f"TSMC source entry failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started)
            return self.result
        try:
            sitemap = await self._sitemap()
        except Exception as exc:
            sitemap = None
            self.result.warnings.append("sitemap_crosscheck_unavailable")
            self.result.metadata["sitemap_crosscheck_error"] = f"{type(exc).__name__}: {exc}"
        self.result.metadata["official_entry"] = entry
        if sitemap is not None:
            self.result.metadata["sitemap_crosscheck_initial"] = {
                key: sitemap[key] for key in ("url", "loc_count", "status", "seconds",
                                              "response_sha256", "response_bytes", "issues", "attempts")}
            self.result.metadata["sitemap_crosscheck_initial"]["job_ids"] = sitemap["ids"]
        else:
            self.result.metadata["sitemap_crosscheck_initial"] = None

        try:
            initial = await self._listing_sweep("initial")
        except Exception as exc:
            self.result.errors.append(f"TSMC initial listing sweep failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started)
            return self.result
        self.result.expected_total = initial["total"]
        self.result.metadata["initial_listing"] = {
            key: initial[key] for key in ("label", "total", "ids", "issues", "seconds")}
        self.result.metadata["initial_listing"]["pages"] = initial["pages"]
        self.result.errors.extend(initial["issues"])
        if entry["results_total"] != initial["total"]:
            self.result.errors.append(
                f"tsmc_entry_and_listing_total_mismatch:{entry['results_total']}!={initial['total']}")
        sitemap_initial_ids = set(sitemap["ids"]) if sitemap is not None else None
        listing_initial_ids = set(initial["ids"])
        if sitemap_initial_ids is not None and sitemap_initial_ids != listing_initial_ids:
            self.result.errors.append("tsmc_sitemap_and_listing_membership_mismatch")
        if sitemap is not None and sitemap["issues"]:
            self.result.errors.extend(sitemap["issues"])

        jobs = []
        for row in initial["rows"]:
            job = self.job(
                {"listing": row},
                title=row["title"],
                url=row["url"],
                job_id=row["job_id"],
                requisition_id=None,
                description="",
                method=None,
                description_url=None,
                locations=[row["listing_location"]] if row.get("listing_location") else [],
                employment_type=row.get("listing_employment_type"),
                date_posted=row.get("posted_date"),
                date_posted_raw=row.get("posted_label"),
            )
            job.record_identity = f"tsmc:{row['job_id']}"
            jobs.append(job)
        if self.options.max_jobs and len(jobs) > self.options.max_jobs:
            jobs = jobs[:self.options.max_jobs]
            self.result.warnings.append("sample_limit")
            self.result.metadata["sample_limit"] = self.options.max_jobs
        detail_started = time.perf_counter()
        hydrated = await self.hydrate(jobs, self._detail)
        detail_wall_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["detail_fetch_wall_seconds"] = detail_wall_seconds
        if self.blocked_reason:
            self.result.errors.append(f"tsmc_detail_collection_blocked:{self.blocked_reason}")
            self.result.metadata["detail_collection_blocked"] = self.blocked_reason

        final = None
        final_sitemap = None
        try:
            final = await self._listing_sweep("final")
        except Exception as exc:
            self.result.errors.append(f"TSMC final listing recheck failed: {type(exc).__name__}: {exc}")
        try:
            final_sitemap = await self._sitemap()
        except Exception as exc:
            self.result.warnings.append("final_sitemap_crosscheck_unavailable")
            self.result.metadata["final_sitemap_crosscheck_error"] = f"{type(exc).__name__}: {exc}"
        if final is None:
            final_matches = False
        else:
            final_ids = final["ids"]
            initial_ids = initial["ids"]
            final_by_id = {row["job_id"]: row for row in final["rows"]}
            initial_by_id = {row["job_id"]: row for row in initial["rows"]}
            changed_fields = {}
            for identity in initial_by_id:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key))
            totals_match = initial["total"] == final["total"] == entry["results_total"]
            same_id_set = set(initial_ids) == set(final_ids)
            same_order = initial_ids == final_ids
            sitemap_sets_match = None
            if sitemap is not None and final_sitemap is not None:
                sitemap_sets_match = set(final_sitemap["ids"]) == set(final_ids) == set(sitemap["ids"])
            final_matches = (totals_match and same_id_set
                             and (sitemap_sets_match is not False)
                             and not changed_fields and not final["issues"]
                             and not (final_sitemap or {}).get("issues"))
            self.result.metadata["final_listing"] = {
                key: final[key] for key in ("label", "total", "ids", "issues", "seconds")}
            self.result.metadata["final_listing"]["pages"] = final["pages"]
            if final_sitemap is not None:
                self.result.metadata["sitemap_crosscheck_final"] = {
                    key: final_sitemap[key] for key in ("url", "loc_count", "status", "seconds",
                                                        "response_sha256", "response_bytes", "issues",
                                                        "attempts")}
                self.result.metadata["sitemap_crosscheck_final"]["job_ids"] = final_sitemap["ids"]
            else:
                self.result.metadata["sitemap_crosscheck_final"] = None
            self.result.metadata["membership_reconciliation"] = {
                "initial_total": initial["total"],
                "final_total": final["total"],
                "entry_total": entry["results_total"],
                "initial_pages": len(initial["pages"]),
                "final_pages": len(final["pages"]),
                "initial_unique_ids": len(set(initial_ids)),
                "final_unique_ids": len(set(final_ids)),
                "sitemap_job_ids": len(sitemap["ids"]) if sitemap is not None else None,
                "final_sitemap_job_ids": len(final_sitemap["ids"]) if final_sitemap is not None else None,
                "sitemap_crosscheck_available": sitemap_sets_match is not None,
                "same_id_set": same_id_set,
                "same_order": same_order,
                "sitemap_sets_match": sitemap_sets_match,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final_ids if i not in set(initial_ids)],
                "removed_job_ids": [i for i in initial_ids if i not in set(final_ids)],
                "complete": final_matches,
            }
            if not totals_match:
                self.result.errors.append("tsmc_listing_total_changed_during_collection")
            if not same_id_set:
                self.result.errors.append("tsmc_listing_membership_changed_during_collection")
            if sitemap_sets_match is False:
                self.result.errors.append("tsmc_sitemap_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("tsmc_listing_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])
            if final_sitemap is not None:
                self.result.errors.extend(final_sitemap["issues"])
        if final is not None and not self.result.metadata["membership_reconciliation"]["same_order"]:
            self.result.metadata["listing_order_changed"] = True

        self.result.jobs = hydrated
        self.result.metadata["job_ids"] = [job.job_id for job in hydrated]
        self.result.metadata["collected_count"] = len(hydrated)
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(
            final_matches and len(hydrated) == initial["total"]
            and all(job.description_verified for job in hydrated))
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        self.result.metadata["source_collection_wall_seconds"] = self.result.timings[
            "source_collection_wall_seconds"]
        return self.result
