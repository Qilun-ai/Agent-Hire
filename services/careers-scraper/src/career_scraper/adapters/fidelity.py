"""Fidelity's own careers site (English/Ireland/India editions) and its published Temporary roles site.

Scope: the routes published by https://jobs.fidelity.com/ itself --
the English worldwide search, the Ireland and India edition searches exposed
by the site's own edition selector, and the "Temporary roles at Fidelity"
link to the www.fidelitytalentsource.com job search. No aggregator, guessed
ATS slug or parent-company-wide board is used.
"""
from __future__ import annotations

from hashlib import sha256
import math
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://jobs.fidelity.com/"
MAIN_LISTING = "https://jobs.fidelity.com/en/jobs/"
IRELAND_LISTING = "https://jobs.fidelity.com/ie/jobs/"
INDIA_LISTING = "https://jobs.fidelity.com/in/jobs/"
TEMPORARY_LISTING = "https://www.fidelitytalentsource.com/job-search/"
TEMPORARY_ORIGIN = "https://www.fidelitytalentsource.com"

PAGE_SIZE = 20
# The site serves the real paginated listing to an ordinary browser UA; the
# generic scraper UA receives page 1 content for every ?page value.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
POSTED_LABEL = re.compile(r"Posted\s+([A-Z][a-z]{2}\s+\d{1,2})")
TOTAL_TEXT = re.compile(r"([\d,]+)\s+(?:open roles|jobs available)")
STATIC_SECTIONS = (
    "fcs-job-form",
    "fcs-job-desc__benefits",
    "fcs-job-overview__body",
)

JOURNEYS = (
    {
        "key": "english_worldwide",
        "edition": "en",
        "label": "Search jobs (English worldwide edition)",
        "listing_url": MAIN_LISTING,
        "host": "jobs.fidelity.com",
        "kind": "fidelity",
    },
    {
        "key": "ireland",
        "edition": "ie",
        "label": "Search jobs (Ireland edition, published by the site's edition selector)",
        "listing_url": IRELAND_LISTING,
        "host": "jobs.fidelity.com",
        "kind": "fidelity",
    },
    {
        "key": "india",
        "edition": "in",
        "label": "Search jobs (India edition, published by the site's edition selector)",
        "listing_url": INDIA_LISTING,
        "host": "jobs.fidelity.com",
        "kind": "fidelity",
    },
    {
        "key": "temporary_roles",
        "edition": "ts",
        "label": "Temporary roles at Fidelity (company-published Fidelity TalentSource search)",
        "listing_url": TEMPORARY_LISTING,
        "host": "www.fidelitytalentsource.com",
        "kind": "talentsource",
    },
)

SECTION_RESPONSIBILITY = ("responsibilit", "the role", "what you will do", "what you'll do")
SECTION_QUALIFICATION = ("expertise", "skills", "qualif", "requirement", "what you bring",
                         "who you are")


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_html_text(html: str | None) -> str:
    return clean(text_html(html or ""))


def decode_cloudflare_email(hex_value: str) -> str | None:
    """Decode Cloudflare's public email obfuscation attribute (not a bot challenge)."""
    try:
        key = int(hex_value[:2], 16)
        return "".join(chr(int(hex_value[index:index + 2], 16) ^ key)
                       for index in range(2, len(hex_value), 2))
    except (TypeError, ValueError):
        return None


def cloudflare_emails(soup: BeautifulSoup) -> list[dict]:
    """Record the public e-mail behind Cloudflare's display obfuscation without altering displayed text."""
    emails = []
    for span in soup.select("span.__cf_email__[data-cfemail]"):
        value = span.get("data-cfemail", "")
        email = decode_cloudflare_email(value)
        if email:
            emails.append({"data_cfemail": value, "email": email})
    return emails


def _find_jobposting(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            import json
            value = json.loads(raw)
        except ValueError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    raise ValueError("Fidelity detail no longer exposes a schema.org JobPosting JSON-LD block")


def _jsonld_locations(value) -> list[dict]:
    values = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    locations = []
    for item in values:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        locations.append({
            "name": clean(item.get("name")),
            "address": address if isinstance(address, dict) else None,
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
        })
    return locations


def _jsonld_employer(value) -> str | None:
    if isinstance(value, dict):
        return clean(value.get("name")) or None
    if isinstance(value, str):
        return clean(value) or None
    return None


def parse_official_home(html: str, response_url: str = "https://jobs.fidelity.com/en/") -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "jobs.fidelity.com"
            or parsed.path.rstrip("/") not in {"", "/en"}):
        raise ValueError("Fidelity official entry redirected away from the inspected careers home")
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Fidelity" not in title:
        raise ValueError("Fidelity official entry company identity changed")
    links = [(clean(a.get_text(" ", strip=True)), urljoin(response_url, a.get("href", "")))
             for a in soup.select("a[href]")]
    search = sorted({url for text, url in links if text.lower() == "search jobs" and "/jobs/" in url})
    temporary = sorted({url for text, url in links if "temporary roles" in text.lower()})
    if MAIN_LISTING not in search:
        raise ValueError("Fidelity careers home no longer publishes the exact Search jobs route")
    temporary_published = [url for url in temporary
                           if url.rstrip("/") in {"https://www.fidelitytalentsource.com",
                                                  TEMPORARY_LISTING.rstrip("/")}]
    if not temporary_published:
        raise ValueError("Fidelity careers home no longer publishes the exact Temporary roles route")
    return {
        "url": response_url,
        "title": title,
        "search_jobs_links": search,
        "temporary_roles_links": temporary_published,
        "temporary_roles_resolved_listing": TEMPORARY_LISTING,
        "edition_links": sorted({url for text, url in links
                                 if re.fullmatch(r"/(?:en|ie|in)/jobs/", urlsplit(url).path)}),
    }


def parse_listing_page(html: str, response_url: str) -> dict:
    """Parse one server-rendered Fidelity search results page."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc not in {"jobs.fidelity.com", "www.fidelitytalentsource.com"}:
        raise ValueError("Fidelity listing response came from an unexpected host")
    soup = BeautifulSoup(html, "lxml")
    holder = soup.select_one("#js-job-search-results")
    count_node = soup.select_one(".job-count")
    issues = []
    total = None
    if holder is not None and str(holder.get("data-results", "")).isdigit():
        total = int(holder["data-results"])
    elif count_node is not None:
        match = TOTAL_TEXT.search(count_node.get_text(" ", strip=True))
        if match:
            total = int(match.group(1).replace(",", ""))
    if total is None:
        issues.append("fidelity_listing_total_missing")
    if soup.select_one(".card-job") is None and total not in (None, 0):
        issues.append("fidelity_listing_has_no_job_cards")
    if soup.select_one('input[type="search"], input[name="search"]') is not None:
        keyword_search = True
    else:
        keyword_search = False
    filter_controls = [clean(select.get("name") or select.get("id") or "")
                       for select in soup.select("form select")]
    rows, seen = [], set()
    for card in soup.select("div.card.card-job"):
        anchor = card.select_one("a.stretched-link.js-view-job")
        if anchor is None:
            issues.append("fidelity_listing_card_missing_job_link")
            continue
        identity = clean(card.get("data-id"))
        href = urljoin(response_url, anchor.get("href", ""))
        if not identity or not re.fullmatch(r"\d+", identity):
            issues.append("fidelity_listing_card_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"fidelity_listing_duplicate_id:{identity}")
            continue
        seen.add(identity)
        items = [clean(li.get_text(" ", strip=True)) for li in card.select("ul.job-meta > li")]
        classes = [li.get("class") or [] for li in card.select("ul.job-meta > li")]
        workplace = next((text for text, cls in zip(items, classes)
                          if "work-pattern" in cls and text), None)
        posted_label = None
        department_label = None
        rest = []
        for text, cls in zip(items, classes):
            if "work-pattern" in cls:
                continue
            match = POSTED_LABEL.search(text)
            if match and posted_label is None:
                posted_label = match.group(0)
                prefix = clean(text[:match.start()])
                if prefix:
                    department_label = prefix
                continue
            if text:
                rest.append(text)
        if department_label is None and rest:
            department_label = rest.pop(0)
        location_label = rest[0] if rest else None
        rows.append({
            "id": identity,
            "url": href,
            "title": clean(anchor.get_text(" ", strip=True)),
            "posted_label": posted_label,
            "department_label": department_label,
            "location_label": location_label,
            "workplace_label": workplace,
            "meta": items,
        })
    return {
        "url": response_url,
        "total": total,
        "rows": rows,
        "issues": issues,
        "keyword_search_available": keyword_search,
        "filter_controls": filter_controls,
        "page_size": len(rows),
    }


def _visible_meta_rows(soup: BeautifulSoup) -> dict[str, str]:
    values = {}
    for row in soup.select(".fcs-job-desc__details .fcs-job-desc__meta-row"):
        title = row.select_one(".fcs-job-desc__meta-title")
        if title is None:
            continue
        label = clean(title.get_text(" ", strip=True))
        items = [clean(li.get_text(" ", strip=True)) for li in row.select("li")]
        items = [item for item in items if item]
        values[label] = " | ".join(items) if items else clean(
            row.get_text(" ", strip=True)[len(label):]
        )
    return values


def _section_rows(job_description_html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-fidelity-body="1">{job_description_html or ""}</div>', "lxml")
    root = soup.select_one("[data-fidelity-body]")
    if root is None:
        return []
    sections, current = [], None
    for block in root.contents:
        if not isinstance(block, Tag):
            continue
        heading = ""
        long_heading_body = False
        if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            candidate = clean(block.get_text(" ", strip=True))
            if len(candidate) <= 140:
                heading = candidate
            else:
                long_heading_body = True
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            text = clean(block.get_text(" ", strip=True))
            if strong is not None and clean(strong.get_text(" ", strip=True)) == text and len(text) <= 120:
                heading = text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        if long_heading_body:
            current["html"] += f"<p>{block.get_text(' ', strip=True)}</p>"
        else:
            current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = normalized_html_text(section["html"])
    return sections


def _split_sections(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    responsibilities, qualifications = [], []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if not heading or len(heading) > 140:
            continue
        item = {"heading": section["heading"], "text": section["text"]}
        if any(token in heading for token in SECTION_RESPONSIBILITY):
            responsibilities.append(item)
        if any(token in heading for token in SECTION_QUALIFICATION):
            qualifications.append(item)
    return responsibilities, qualifications


def parse_fidelity_detail(html: str, response_url: str) -> dict:
    """Parse one jobs.fidelity.com job page including its rendered description article."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "jobs.fidelity.com":
        raise ValueError("Fidelity detail response came from an unexpected host")
    soup = BeautifulSoup(html, "lxml")
    posting = _find_jobposting(soup)
    article = soup.select_one("article.fcs-job-desc__content")
    if article is None:
        raise ValueError("Fidelity detail no longer exposes article.fcs-job-desc__content")
    identifier = clean(str(posting.get("identifier") or ""))
    if not re.fullmatch(r"\d+", identifier):
        raise ValueError("Fidelity detail JobPosting identifier is missing")
    job_body_html = posting.get("description") or ""
    job_body_text = normalized_html_text(job_body_html)

    # The article also contains the rendered pre-application form and the
    # shared benefits/company-policy blocks. Keep the displayed article body
    # exactly; remove only the "Return to job search" navigation link.
    obfuscated_emails = cloudflare_emails(article)
    for back in article.select("a.fcs-job-desc__back"):
        back.decompose()
    description_html = "".join(str(node) for node in article.contents)
    description_text = text_html(description_html)
    form = article.select_one("#preApplicationForm")
    apply_url = clean(form.get("data-redirect-url")) if form is not None else None
    tooltip = soup.select_one(".fcs-work-pattern [data-shift]")
    work_nodes = [clean(node.get_text(" ", strip=True))
                  for node in soup.select(".fcs-work-pattern p")]
    workplace = next((text for text in work_nodes
                      if text.lower() in {"onsite", "hybrid", "remote"}), None)
    time_node = soup.select_one(".fcs-job-desc__id time[datetime]")
    id_node = soup.select_one(".fcs-job-desc__id")
    visible_id = None
    if id_node is not None:
        match = re.search(r"ID:\s*(\d+)", id_node.get_text(" ", strip=True))
        visible_id = match.group(1) if match else None
    category = soup.select_one(".fcs-job-desc__category")
    title_node = soup.select_one("h1.fcs-job-desc__title")
    parts_title = clean(title_node.get_text(" ", strip=True)) if title_node else ""
    meta_rows = _visible_meta_rows(soup)
    sections = _section_rows(job_body_html)
    responsibilities, qualifications = _split_sections(sections)
    jsonld_without_description = {key: value for key, value in posting.items() if key != "description"}
    return {
        "kind": "fidelity",
        "url": response_url,
        "identifier": identifier,
        "visible_identifier": visible_id,
        "title": clean(posting.get("title")) or parts_title,
        "visible_title": parts_title,
        "category": clean(category.get_text(" ", strip=True)) if category else None,
        "team": meta_rows.get("Team"),
        "experience_level": meta_rows.get("Experience Level"),
        "visible_location": meta_rows.get("Location"),
        "workplace_type": workplace,
        "time_onsite": clean(tooltip.get("data-shift")) if tooltip else None,
        "time_onsite_note": next((text for text in work_nodes if text.lower() not in {"onsite", "hybrid", "remote"}), None),
        "date_posted": clean(posting.get("datePosted")) or None,
        "date_label": clean(time_node.get_text(" ", strip=True)) if time_node else None,
        "element_datetime": time_node.get("datetime") if time_node else None,
        "employment_type": clean(posting.get("employmentType")) or None,
        "industry": clean(posting.get("industry")) or None,
        "locations": _jsonld_locations(posting.get("jobLocation")),
        "employer": _jsonld_employer(posting.get("hiringOrganization")),
        "base_salary": posting.get("baseSalary"),
        "valid_through": clean(posting.get("validThrough")) or None,
        "jsonld": jsonld_without_description,
        "job_body_html": job_body_html,
        "job_body_text": job_body_text,
        "description_html": description_html,
        "description_text": description_text,
        "description_method": "fidelity.jobs.article.fcs-job-desc__content",
        "apply_url": apply_url,
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "application_form_present": form is not None,
        "application_form_fields": [clean(el.get("name") or "") for el in form.select("input,select")
                                    if form is not None] if form is not None else [],
        "back_link_removed": "Return to job search" not in normalized_html_text(description_html),
        "cloudflare_emails": obfuscated_emails,
    }


def parse_talentsource_detail(html: str, response_url: str) -> dict:
    """Parse one www.fidelitytalentsource.com job page."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.fidelitytalentsource.com":
        raise ValueError("Fidelity TalentSource detail response came from an unexpected host")
    soup = BeautifulSoup(html, "lxml")
    posting = _find_jobposting(soup)
    article = soup.select_one("article.cms-content")
    if article is None:
        raise ValueError("Fidelity TalentSource detail no longer exposes article.cms-content")
    obfuscated_emails = cloudflare_emails(article)
    identifier = clean(str(posting.get("identifier") or ""))
    if not re.fullmatch(r"\d+", identifier):
        raise ValueError("Fidelity TalentSource JobPosting identifier is missing")
    job_body_html = posting.get("description") or ""
    description_html = "".join(str(node) for node in article.contents)
    description_text = text_html(description_html)
    apply_anchor = soup.select_one('a[href="#js-application"]')
    application = soup.select_one("#js-application")
    meta = [clean(li.get_text(" ", strip=True)) for li in soup.select(".hero-job .job-meta li")]
    posted = next((text for text in meta if POSTED_LABEL.fullmatch(text) or POSTED_LABEL.match(text)), None)
    location = next((text for text in meta if text and not POSTED_LABEL.match(text)), None)
    sections = _section_rows(job_body_html)
    responsibilities, qualifications = _split_sections(sections)
    return {
        "kind": "talentsource",
        "url": response_url,
        "identifier": identifier,
        "visible_identifier": identifier,
        "title": clean(posting.get("title")),
        "visible_title": clean((soup.select_one("h1") or soup).get_text(" ", strip=True))
        if soup.select_one("h1") else clean(posting.get("title")),
        "category": None,
        "team": clean(posting.get("industry")) or None,
        "experience_level": None,
        "visible_location": location,
        "workplace_type": None,
        "time_onsite": None,
        "time_onsite_note": None,
        "date_posted": clean(posting.get("datePosted")) or None,
        "date_label": posted,
        "element_datetime": None,
        "employment_type": clean(posting.get("employmentType")) or None,
        "industry": clean(posting.get("industry")) or None,
        "locations": _jsonld_locations(posting.get("jobLocation")),
        "employer": _jsonld_employer(posting.get("hiringOrganization")),
        "base_salary": posting.get("baseSalary"),
        "valid_through": clean(posting.get("validThrough")) or None,
        "jsonld": {key: value for key, value in posting.items() if key != "description"},
        "job_body_html": job_body_html,
        "job_body_text": normalized_html_text(job_body_html),
        "description_html": description_html,
        "description_text": description_text,
        "description_method": "fidelity.talentsource.article.cms-content",
        "apply_url": None,
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "application_form_present": False,
        "application_form_fields": [],
        "back_link_removed": True,
        "cloudflare_emails": obfuscated_emails,
        "application_control": {
            "label": clean(apply_anchor.get_text(" ", strip=True)) if apply_anchor else None,
            "target": apply_anchor.get("href") if apply_anchor else None,
            "provider": "Beamery" if application is not None else None,
            "convert_company_id": application.get("data-beamery-convert-company-id") if application is not None else None,
            "convert_flow_id": application.get("data-beamery-convert-flow-id") if application is not None else None,
        },
    }


def expected_page_count(total: int | None, page_size: int = PAGE_SIZE) -> int | None:
    if total is None:
        return None
    return math.ceil(total / page_size) if total else 0


class Fidelity(Adapter):
    """Collect every role from Fidelity's own published search routes."""

    VERIFICATION_REVISION = "fidelity.jobs-native-multiedition.1"

    def __init__(self, http, company, source, options, *, journeys=None):
        super().__init__(http, company, source, options)
        self.journeys = tuple(journeys or JOURNEYS)

    async def _official_home(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_home(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _listing_page(self, journey, page: int, total: int | None = None):
        listing = journey["listing_url"]
        url = listing if page == 1 else f"{listing}?page={page}"
        last = None
        for attempt in range(3):
            if not self.page_guard():
                raise ValueError("Fidelity listing page budget exhausted")
            started = time.perf_counter()
            response = await self.http.get(url, headers=BROWSER_HEADERS)
            seconds = max(0.0, time.perf_counter() - started)
            parsed = parse_listing_page(response.text, str(response.url))
            parsed.update(request_url=url, final_url=str(response.url), status=response.status_code,
                          seconds=seconds, response_sha256=sha256(response.content).hexdigest(),
                          response_bytes=len(response.content))
            if parsed["total"] is None:
                last = "listing_total_missing"
            elif total is not None and parsed["total"] != total:
                last = f"listing_total_changed:{total}->{parsed['total']}"
            elif parsed["issues"]:
                last = ",".join(parsed["issues"])
            elif total is not None:
                expected = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
                if len(parsed["rows"]) != expected:
                    last = f"listing_page_card_count:{len(parsed['rows'])}!={expected}"
                else:
                    return parsed
            else:
                return parsed
            if attempt < 2:
                await self._sleep(2 + attempt * 2)
        raise ValueError(f"Fidelity listing page {url} invalid after retries: {last}")

    @staticmethod
    async def _sleep(seconds: float):
        import asyncio
        await asyncio.sleep(seconds)

    async def _listing_snapshot(self, journey):
        last = None
        for attempt in range(2):
            last = await self._listing_snapshot_once(journey)
            if not last["issues"]:
                return last
            if attempt == 0:
                await self._sleep(2)
        return last

    async def _listing_snapshot_once(self, journey):
        started = time.perf_counter()
        first = await self._listing_page(journey, 1)
        total = first["total"]
        page_size = len(first["rows"]) or PAGE_SIZE
        pages_expected = expected_page_count(total, page_size)
        rows = list(first["rows"])
        pages = [{key: first[key] for key in ("request_url", "final_url", "status", "seconds",
                                              "response_sha256", "response_bytes", "total")}
                 | {"page": 1, "job_ids": [row["id"] for row in first["rows"]]}]
        issues = list(first["issues"])
        for page in range(2, (pages_expected or 1) + 1):
            parsed = await self._listing_page(journey, page, total)
            rows.extend(parsed["rows"])
            pages.append({key: parsed[key] for key in ("request_url", "final_url", "status", "seconds",
                                                       "response_sha256", "response_bytes", "total")}
                         | {"page": page, "job_ids": [row["id"] for row in parsed["rows"]]})
            issues.extend(parsed["issues"])
        ids = [row["id"] for row in rows]
        seen = set()
        duplicates = sorted({identity for identity in ids if identity in seen or seen.add(identity)})
        if duplicates:
            issues.append("fidelity_listing_duplicate_ids:" + ",".join(duplicates))
        if total is not None and len(set(ids)) != total:
            issues.append(f"fidelity_listing_total_mismatch:{total}!={len(set(ids))}")
        return {
            "journey": journey["key"],
            "listing_url": journey["listing_url"],
            "total": total,
            "page_size": page_size,
            "pages_expected": pages_expected,
            "pages": pages,
            "job_ids": ids,
            "rows": rows,
            "issues": issues,
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _listing_job(self, journey, row):
        return self.job(
            {"journey": journey["key"], "listing": row},
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            description="",
            method=None,
            description_url=None,
        )

    async def _detail(self, job):
        journey = job.raw["journey"]
        url = job.url
        last = None
        for attempt in range(3):
            started = time.perf_counter()
            try:
                response = await self.http.get(url, headers=BROWSER_HEADERS)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    await self._sleep(2 + attempt * 2)
                continue
            seconds = max(0.0, time.perf_counter() - started)
            try:
                if journey == "temporary_roles":
                    detail = parse_talentsource_detail(response.text, str(response.url))
                else:
                    detail = parse_fidelity_detail(response.text, str(response.url))
            except ValueError as exc:
                last = str(exc)
                if attempt < 2:
                    await self._sleep(2 + attempt * 2)
                continue
            detail.update(status=response.status_code, request_url=url, final_url=str(response.url),
                          request_seconds=seconds, response_sha256=sha256(response.content).hexdigest(),
                          response_bytes=len(response.content))
            return self._job_from_detail(journey, job.raw["listing"], detail)
        raise ValueError(f"Fidelity detail failed after retries for {url}: {last}")

    def _job_from_detail(self, journey, row, detail):
        raw = {
            "journey": journey,
            "listing": row,
            "jsonld": detail["jsonld"],
            "jsonld_description_sha256": sha256((detail["job_body_html"] or "").encode()).hexdigest(),
            "job_body_text_sha256": sha256((detail["job_body_text"] or "").encode()).hexdigest(),
            "job_body_normalized_chars": len(detail["job_body_text"] or ""),
            "visible": {
                "title": detail["visible_title"],
                "category": detail["category"],
                "team": detail["team"],
                "experience_level": detail["experience_level"],
                "location": detail["visible_location"],
                "workplace_type": detail["workplace_type"],
                "time_onsite": detail["time_onsite"],
                "time_onsite_note": detail["time_onsite_note"],
                "date_label": detail["date_label"],
                "element_datetime": detail["element_datetime"],
                "identifier": detail["visible_identifier"],
            },
            "description": {
                "article_includes_application_form": detail["application_form_present"],
                "application_form_fields": detail["application_form_fields"],
                "back_link_removed": detail["back_link_removed"],
                "cloudflare_emails": detail["cloudflare_emails"],
                "description_sha256": sha256((detail["description_text"] or "").encode()).hexdigest(),
                "description_normalized_chars": len(detail["description_text"] or ""),
                "sections": detail["sections"],
            },
            "application_route": {
                "detail_url": detail["url"],
                "workday_apply_url": detail["apply_url"],
                "application_control": detail.get("application_control"),
            },
            "detail_request": {key: detail[key] for key in
                              ("request_url", "final_url", "status", "request_seconds",
                               "response_sha256", "response_bytes")},
        }
        job = self.job(
            raw,
            title=detail["title"],
            url=detail["url"],
            job_id=detail["identifier"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method=detail["description_method"],
            description_url=detail["url"],
            locations=detail["locations"],
            department=detail["industry"] or detail["category"],
            team=detail["team"],
            employment_type=detail["employment_type"],
            workplace_type=detail["workplace_type"],
            date_posted=detail["date_posted"],
            date_posted_raw={
                "jsonld_datePosted": detail["date_posted"],
                "visible_label": detail["date_label"],
                "visible_datetime_attribute": detail["element_datetime"],
                "listing_label": row.get("posted_label"),
            },
            date_updated=None,
            valid_through=detail["valid_through"],
            salary=detail["base_salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            employer=detail["employer"],
            language="en",
        )
        job.description_text = detail["description_text"]
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": MAIN_LISTING,
            "listing_authority": (
                "https://jobs.fidelity.com/ publishes 'Search jobs' (/en/jobs/), the English/Ireland/India "
                "edition selector, and 'Temporary roles at Fidelity' (www.fidelitytalentsource.com)."
            ),
            "journeys": [
                {"key": journey["key"], "label": journey["label"], "listing_url": journey["listing_url"]}
                for journey in self.journeys
            ],
            "detail_method": (
                "One GET per exact card detail URL; description from the rendered article body "
                "(article.fcs-job-desc__content for Fidelity editions, article.cms-content for "
                "Fidelity TalentSource), with datePosted/identifier/locations from JobPosting JSON-LD."
            ),
        })
        try:
            entry = await self._official_home()
        except Exception as exc:
            self.result.errors.append(f"Fidelity official entry failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.metadata["official_entry"] = entry

        initial, failures = {}, []
        collection_started = time.perf_counter()
        for journey in self.journeys:
            try:
                snapshot = await self._listing_snapshot(journey)
            except Exception as exc:
                failures.append({"journey": journey["key"], "listing_url": journey["listing_url"],
                                 "reason": f"{type(exc).__name__}: {exc}"})
                self.result.errors.append(f"Fidelity listing failed ({journey['key']}): {exc}")
                continue
            initial[journey["key"]] = snapshot
            self.result.timings[f"listing_{journey['key']}_seconds"] = snapshot["request_seconds"]
            if snapshot["issues"]:
                self.result.errors.extend(f"{journey['key']}: {issue}" for issue in snapshot["issues"])
        self.result.timings["listing_phase_seconds"] = max(0.0, time.perf_counter() - collection_started)
        self.result.metadata["listing_failures"] = failures
        if failures:
            self.result.listing_complete = False

        jobs, order = [], []
        for journey in self.journeys:
            snapshot = initial.get(journey["key"])
            if snapshot is None:
                continue
            for row in snapshot["rows"]:
                order.append(row["id"])
                jobs.append(self._listing_job(journey, row))
        expected_total = sum(snapshot["total"] for snapshot in initial.values()
                             if snapshot["total"] is not None)
        self.result.expected_total = expected_total or None
        self.result.metadata["initial_listing"] = {
            key: {field: value for field, value in snapshot.items() if field != "rows"}
            for key, snapshot in initial.items()
        }
        self.result.metadata["initial_job_ids"] = order
        self.result.metadata["advertised_jobs_by_journey"] = {
            key: snapshot["total"] for key, snapshot in initial.items()
        }

        # Details for every listed role.
        await self.hydrate(jobs, self._detail)

        mismatches, detail_failures, date_label_differences = [], [], []
        for job in jobs:
            identity = str(job.job_id)
            if not job.description_verified:
                detail_failures.append({
                    "id": identity,
                    "url": job.url,
                    "reason": ",".join(job.warnings) or "description_not_verified",
                })
                self.result.errors.append(f"fidelity_description_not_verified:{identity}")
            row = job.raw.get("listing") or {}
            if clean(job.title).casefold() != clean(row.get("title")).casefold():
                mismatches.append({"id": identity, "field": "title",
                                   "listing": row.get("title"), "detail": job.title})
            if job.date_posted and row.get("posted_label"):
                label = POSTED_LABEL.search(row["posted_label"])
                if label:
                    expected_label = f"{job.date_posted[5:7]}-{job.date_posted[8:10]}"
                    observed = label.group(1).split()
                    if len(observed) == 2 and f"{date_month(observed[0])}-{observed[1].zfill(2)}" != expected_label:
                        date_label_differences.append({
                            "id": identity, "listing_label": row["posted_label"],
                            "detail_published": job.date_posted,
                            "detail_visible_label": (job.date_posted_raw or {}).get("visible_label"),
                        })
            if job.workplace_type and row.get("workplace_label") and \
                    clean(job.workplace_type).casefold() != clean(row["workplace_label"]).casefold():
                mismatches.append({"id": identity, "field": "workplace_type",
                                   "listing": row["workplace_label"], "detail": job.workplace_type})
        if detail_failures:
            self.result.metadata["failed_jobs"] = detail_failures
        if mismatches:
            self.result.metadata["listing_detail_mismatches"] = mismatches
            self.result.warnings.append("listing_detail_field_mismatches")
        if date_label_differences:
            self.result.metadata["listing_card_posted_vs_detail_published"] = {
                "count": len(date_label_differences),
                "explanation": (
                    "The listing card's 'Posted' label can be a later re-post/refresh date than the "
                    "detail page's 'Published' date and JobPosting datePosted. date_posted uses the "
                    "detail's published/datePosted value; both labels are preserved in date_posted_raw."
                ),
                "differences": date_label_differences,
            }

        # Final membership recheck: every published page is read again.
        final, final_failures = {}, []
        reconcile_started = time.perf_counter()
        for journey in self.journeys:
            try:
                snapshot = await self._listing_snapshot(journey)
            except Exception as exc:
                final_failures.append({"journey": journey["key"], "listing_url": journey["listing_url"],
                                       "reason": f"{type(exc).__name__}: {exc}"})
                self.result.errors.append(f"Fidelity final listing failed ({journey['key']}): {exc}")
                continue
            final[journey["key"]] = snapshot
        self.result.timings["final_reconciliation_seconds"] = max(
            0.0, time.perf_counter() - reconcile_started
        )
        reconciliation = {"journeys": {}, "new_job_ids": [], "removed_job_ids": [],
                          "complete": False, "final_failures": final_failures}
        if not final_failures and set(final) == set(initial):
            same = True
            for journey in self.journeys:
                key = journey["key"]
                before, after = initial[key], final[key]
                ids_before, ids_after = before["job_ids"], after["job_ids"]
                new_ids = [identity for identity in ids_after if identity not in set(ids_before)]
                removed_ids = [identity for identity in ids_before if identity not in set(ids_after)]
                before_rows = {row["id"]: row for row in before["rows"]}
                after_rows = {row["id"]: row for row in after["rows"]}
                same_fields = before_rows == after_rows
                reconciliation["journeys"][key] = {
                    "initial_total": before["total"],
                    "final_total": after["total"],
                    "initial_pages": before["pages_expected"],
                    "final_pages": after["pages_expected"],
                    "same_ids": set(ids_before) == set(ids_after),
                    "same_ids_and_order": ids_before == ids_after,
                    "same_rows_by_id": same_fields,
                    "new_job_ids": new_ids,
                    "removed_job_ids": removed_ids,
                }
                reconciliation["new_job_ids"].extend(new_ids)
                reconciliation["removed_job_ids"].extend(removed_ids)
                if not (before["total"] == after["total"]
                        and set(ids_before) == set(ids_after) and same_fields):
                    same = False
            reconciliation["complete"] = same and not any(
                snapshot["issues"] for snapshot in final.values())
            self.result.metadata["final_listing"] = {
                key: {field: value for field, value in snapshot.items() if field != "rows"}
                for key, snapshot in final.items()
            }
            self.result.metadata["final_job_ids"] = [
                identity for journey in self.journeys
                for identity in final.get(journey["key"], {}).get("job_ids", [])
            ]
            if not reconciliation["complete"]:
                self.result.errors.append("fidelity_listing_membership_changed_during_collection")
        else:
            reconciliation["complete"] = False
            self.result.errors.append("fidelity_final_listing_reconciliation_incomplete")
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["current_native_membership_reconciled"] = reconciliation["complete"]

        verified = sum(job.description_verified for job in jobs)
        self.result.metadata["jobs_collected"] = len(jobs)
        self.result.metadata["descriptions_verified"] = verified
        self.result.metadata["detail_request_count"] = sum(
            1 for job in jobs if job.raw.get("detail_request")
        )
        self.result.metadata["whole_record_details_requested_per_job"] = 1
        self.result.jobs = jobs
        self.result.listing_complete = bool(
            reconciliation["complete"]
            and expected_total == len(jobs) == len(set(job.job_id for job in jobs))
            and not failures
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result


MONTHS = {month: index for index, month in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def date_month(label: str) -> str:
    return f"{MONTHS.get(label[:3], 0):02d}"
