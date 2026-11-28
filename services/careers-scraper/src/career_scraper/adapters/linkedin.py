"""LinkedIn's own official careers journey and its company-scoped job search.

The marketing site ``careers.linkedin.com`` publishes a ``Search Jobs`` CTA to
LinkedIn's own company-jobs page.  That page publishes exactly one ``See all
jobs`` route filtered to LinkedIn's three published company entities
(LinkedIn, Drawbridge and Lynda.com), and the worldwide listing is the
membership authority.  The listing server-renders its first 60 cards and then
uses LinkedIn's public guest ``seeMoreJobPostings`` endpoint for subsequent
10-card pages.  Each exact guest ``jobPosting/<id>`` fragment contains the
employer-authored body, while the listing card supplies the exact posting date.

No guessed LinkedIn company slug, parent-wide board, aggregator search or
logged-in route is used.  Bot challenges / 429 responses are recorded and stop
further guest requests; they are never solved.
"""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import gzip
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..http import FetchError
from ..models import now, text_html


OFFICIAL_ENTRY = "https://careers.linkedin.com/"
COMPANY_JOBS_URL = "https://www.linkedin.com/company/linkedin/jobs/"
LISTING_HOST = "www.linkedin.com"
LISTING_PATH = "/jobs/linkedin-jobs-worldwide"
LISTING_URL = "https://www.linkedin.com/jobs/linkedin-jobs-worldwide?f_C=2587638%2C39939%2C1337"
GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
LINKEDIN_HOSTS = {"www.linkedin.com", "in.linkedin.com", "ie.linkedin.com"}

def _is_linkedin_host(host: str | None) -> bool:
    return bool(host) and (host == "linkedin.com" or host.endswith(".linkedin.com"))
ALLOWED_COMPANIES = {"LinkedIn", "Drawbridge (acquired by LinkedIn)", "Lynda.com"}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "HeadlessChrome";v="156"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}
JOB_VIEW_PATH = re.compile(r"^/jobs/view/(?:[^/?#]*-)?(\d+)/?$")
JOB_VIEW_ID = re.compile(r"-(\d+)$")
CARD_URN = re.compile(r"urn:li:jobPosting:(\d+)")
COMPANY_FILTER = re.compile(r"^(?:LinkedIn|Drawbridge \(acquired by LinkedIn\)|Lynda\.com)$")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_RENDERED_BLOCK_TAGS = frozenset({
    "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
})


def _soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _norm(value: str | None) -> str:
    return " ".join((value or "").split())


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _iso_date(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _filter_ids(url: str) -> tuple[str, ...]:
    values = parse_qs(urlsplit(url).query).get("f_C", [])
    if len(values) != 1:
        raise ValueError("LinkedIn published jobs URL has no single company filter")
    ids = tuple(part.strip() for part in values[0].split(",") if part.strip())
    if not ids or any(not part.isdigit() for part in ids):
        raise ValueError("LinkedIn published jobs URL has an invalid company filter")
    return ids


def _filter_text(ids: tuple[str, ...]) -> str:
    return ",".join(ids)


def _job_url(href: str, page_url: str, job_id: str) -> tuple[str, str]:
    absolute = urljoin(page_url, href)
    parsed = urlsplit(absolute)
    match = JOB_VIEW_PATH.fullmatch(parsed.path.rstrip("/"))
    if not _is_linkedin_host(parsed.hostname) or match is None or match.group(1) != job_id:
        raise ValueError(f"LinkedIn card {job_id} has an unexpected job-view URL: {absolute}")
    published = absolute
    canonical = f"https://www.linkedin.com{parsed.path.rstrip('/')}"
    return published, canonical


def _card_rows(soup: BeautifulSoup, page_url: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for card in soup.select("div.base-card[data-entity-urn]"):
        urn = card.get("data-entity-urn", "")
        identity = CARD_URN.fullmatch(urn)
        if identity is None:
            raise ValueError(f"LinkedIn card has an unexpected entity urn: {urn!r}")
        job_id = identity.group(1)
        anchor = (
            card.select_one("a.base-card__full-link[href]")
            or card.select_one('a[href*="/jobs/view/"]')
        )
        if anchor is None or not anchor.get("href"):
            raise ValueError(f"LinkedIn card {job_id} has no job-view link")
        title_node = card.select_one(".base-search-card__title") or card.find("h3")
        company_node = card.select_one(".base-search-card__subtitle") or card.find("h4")
        location_node = card.select_one(".job-search-card__location")
        time_node = card.select_one("time")
        salary_node = card.select_one(".job-search-card__salary-info, .salary")
        title = _norm(title_node.get_text(" ", strip=True) if title_node else "")
        company = _norm(company_node.get_text(" ", strip=True) if company_node else "")
        if not title:
            raise ValueError(f"LinkedIn card {job_id} has no title")
        if company not in ALLOWED_COMPANIES:
            raise ValueError(f"LinkedIn result {job_id} has foreign company name {company!r}")
        if job_id in seen:
            raise ValueError(f"LinkedIn listing repeats job id {job_id}")
        seen.add(job_id)
        published_url, canonical_url = _job_url(anchor["href"], page_url, job_id)
        row = {
            "job_id": job_id,
            "title": title,
            "company": company,
            "location": _norm(location_node.get_text(" ", strip=True)) if location_node else None,
            "job_url": canonical_url,
            "job_url_as_published": published_url,
            "date_posted_raw": time_node.get("datetime") if time_node else None,
            "posted_text": _norm(time_node.get_text(" ", strip=True)) if time_node else None,
            "salary_text": _norm(salary_node.get_text(" ", strip=True)) if salary_node else None,
            "position": int(card.get("data-row")) if str(card.get("data-row") or "").isdigit() else None,
            "page_num": parse_qs(urlsplit(published_url).query).get("pageNum", [None])[0],
            "badges": [_norm(item.get_text(" ", strip=True)) for item in card.select(".job-posting-benefits, .job-search-card__benefits")],
            "source_card_attributes": {k: v for k, v in card.attrs.items() if k.startswith("data-")},
        }
        rows.append(row)
    return rows


def _positions(rows: list[dict], *, start: int) -> list[int] | None:
    values = [row["position"] for row in rows]
    if any(value is None for value in values):
        return None
    return values


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "careers.linkedin.com":
        raise ValueError("LinkedIn careers entry redirected off the official careers host")
    soup = _soup(html)
    title = _norm(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "LinkedIn Careers" not in title:
        raise ValueError(f"LinkedIn careers entry identity changed: {title!r}")
    search_links, location_links = [], []
    for anchor in soup.select("a[href]"):
        label = _norm(anchor.get_text(" ", strip=True)).lower()
        href = urljoin(response_url, anchor["href"])
        target = urlsplit(href)
        if label == "search jobs":
            if target.hostname != "www.linkedin.com" or target.path.rstrip("/") != "/company/linkedin/jobs":
                raise ValueError("LinkedIn Search Jobs CTA no longer points to the official company jobs page")
            search_links.append({"label": "Search Jobs", "url": href})
        elif label == "open roles":
            if not _is_linkedin_host(target.hostname) or not target.path.startswith("/jobs/"):
                raise ValueError("LinkedIn Open roles CTA left the official LinkedIn jobs host")
            location_links.append({
                "label": "Open roles",
                "url": href,
                "location": parse_qs(target.query).get("location", [None])[0],
                "geo_id": parse_qs(target.query).get("geoId", [None])[0],
                "company_filter": list(_filter_ids(href)),
            })
    if len(search_links) != 1:
        raise ValueError(f"LinkedIn careers entry exposes {len(search_links)} Search Jobs CTAs")
    if not location_links:
        raise ValueError("LinkedIn careers entry exposes no location Open roles CTAs")
    return {"title": title, "company_jobs_url": search_links[0]["url"], "location_links": location_links}


def parse_company_jobs(html: str, response_url: str = COMPANY_JOBS_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "www.linkedin.com" or parsed.path.rstrip("/") != "/company/linkedin/jobs":
        raise ValueError("LinkedIn company-jobs response redirected away from the inspected page")
    soup = _soup(html)
    title = _norm(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "LinkedIn" not in title or "Jobs" not in title:
        raise ValueError(f"LinkedIn company-jobs identity changed: {title!r}")
    see_all = []
    for anchor in soup.select("a[href]"):
        label = _norm(anchor.get_text(" ", strip=True)).lower()
        href = urljoin(response_url, anchor["href"])
        target = urlsplit(href)
        if label == "see all jobs" and _is_linkedin_host(target.hostname) and target.path.rstrip("/") == LISTING_PATH:
            see_all.append(href)
    unique_see_all = list(dict.fromkeys(see_all))
    if len(unique_see_all) != 1:
        raise ValueError(f"LinkedIn company jobs page exposes {len(unique_see_all)} distinct See all jobs routes")
    listing_url = unique_see_all[0]
    filter_ids = _filter_ids(listing_url)
    if "1337" not in filter_ids:
        raise ValueError("LinkedIn See all jobs route no longer includes the LinkedIn company id 1337")
    rows = _card_rows(soup, response_url)
    if not rows:
        raise ValueError("LinkedIn company jobs page has no preview cards")
    return {
        "title": title,
        "listing_url": listing_url,
        "filter_ids": filter_ids,
        "preview": rows,
    }


def parse_worldwide_listing(html: str, response_url: str, *, expected_filter: tuple[str, ...]) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or not _is_linkedin_host(parsed.hostname) or parsed.path.rstrip("/") != LISTING_PATH:
        raise ValueError("LinkedIn worldwide listing redirected away from the inspected route")
    if _filter_ids(response_url) != expected_filter:
        raise ValueError("LinkedIn worldwide listing company filter changed")
    soup = _soup(html)
    title = _norm(soup.title.get_text(" ", strip=True)) if soup.title else ""
    match = re.search(r"\((\d[\d,]*)\s+Open Roles\)", title, re.I)
    if not match:
        raise ValueError(f"LinkedIn worldwide listing title has no public Open Roles count: {title!r}")
    total = int(match.group(1).replace(",", ""))
    if total <= 0:
        raise ValueError("LinkedIn worldwide listing returned a zero public count")
    page_text = _norm(soup.get_text(" ", strip=True))
    count_match = re.search(r"LinkedIn jobs\s+(\d[\d,]*)", page_text, re.I)
    if count_match and int(count_match.group(1).replace(",", "")) != total:
        raise ValueError("LinkedIn worldwide listing title and visible job count disagree")
    for company in ("LinkedIn", "Drawbridge (acquired by LinkedIn)", "Lynda.com"):
        if company not in page_text:
            raise ValueError(f"LinkedIn worldwide listing no longer publishes the company filter {company!r}")
    rows = _card_rows(soup, response_url)
    if not rows:
        raise ValueError("LinkedIn worldwide listing has no server-rendered job cards")
    positions = _positions(rows, start=1)
    if positions is None or positions != list(range(1, len(rows) + 1)):
        raise ValueError("LinkedIn worldwide listing card positions are missing, repeated, or out of order")
    if any(row["page_num"] not in (None, "0") for row in rows):
        raise ValueError("LinkedIn worldwide listing server cards are outside pageNum=0")
    if len(rows) > total:
        raise ValueError("LinkedIn worldwide listing has more cards than its public count")
    show_more = soup.select_one("button.infinite-scroller__show-more-button")
    return {
        "title": title,
        "total": total,
        "rows": rows,
        "ids": [row["job_id"] for row in rows],
        "filter_ids": expected_filter,
        "show_more_label": _norm(show_more.get_text(" ", strip=True)) if show_more else None,
        "server_page_num": sorted({row["page_num"] for row in rows}),
    }


def parse_search_fragment(html: str, response_url: str, *, start: int, expected_filter: tuple[str, ...]) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "www.linkedin.com" or parsed.path != "/jobs-guest/jobs/api/seeMoreJobPostings/search":
        raise ValueError("LinkedIn guest search fragment came from an unexpected route")
    if _filter_ids(response_url) != expected_filter:
        raise ValueError("LinkedIn guest search fragment company filter changed")
    if parse_qs(parsed.query).get("start") != [str(start)]:
        raise ValueError("LinkedIn guest search fragment start offset changed")
    soup = _soup(html)
    rows = _card_rows(soup, "https://www.linkedin.com" + LISTING_PATH)
    if not rows:
        return {"start": start, "rows": [], "ids": [], "empty": True}
    positions = _positions(rows, start=start + 1)
    if positions != list(range(start + 1, start + 1 + len(rows))):
        raise ValueError("LinkedIn guest search fragment positions do not continue the listing")
    if len({row["job_id"] for row in rows}) != len(rows):
        raise ValueError("LinkedIn guest search fragment repeats a job id")
    return {"start": start, "rows": rows, "ids": [row["job_id"] for row in rows], "empty": False}


def _criteria(soup: BeautifulSoup) -> tuple[list[dict], dict[str, str]]:
    rows = []
    for item in soup.select("li.description__job-criteria-item"):
        label = item.select_one("h3.description__job-criteria-subheader")
        value = item.select_one("span.description__job-criteria-text")
        if label and value:
            rows.append({"label": _norm(label.get_text(" ", strip=True)), "value": _norm(value.get_text(" ", strip=True))})
    by_label: dict[str, str] = {}
    for row in rows:
        by_label[row["label"]] = f"{by_label[row['label']]}; {row['value']}" if row["label"] in by_label else row["value"]
    return rows, by_label


def _description_inner_html(value: str | None) -> str:
    soup = _soup(value or "")
    markup = soup.select_one(".show-more-less-html__markup")
    if markup is None:
        return ""
    return "".join(str(child) for child in markup.contents)


def _rendered_text_from_html(fragment: str | None) -> str:
    """Close approximation of a browser's innerText for the description body."""
    soup = _soup(fragment or "")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    pieces: list[str] = []

    def add_break() -> None:
        if pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            pieces.append(re.sub(r"\s+", " ", str(node)))
            return
        if not isinstance(node, Tag):
            return
        if node.name in {"script", "style", "noscript"}:
            return
        block = node.name in _RENDERED_BLOCK_TAGS
        if block:
            add_break()
        for child in node.children:
            walk(child)
        if block:
            add_break()

    walk(soup)
    rendered = "".join(pieces)
    rendered = re.sub(r"[\t\f\v\r ]+", " ", rendered)
    rendered = re.sub(r" *\n *", "\n", rendered)
    rendered = re.sub(r"\n+", "\n", rendered)
    return rendered.strip()


def _description_sections(html: str | None) -> list[dict]:
    soup = _soup(f'<div data-linkedin-body="true">{html or ""}</div>')
    root = soup.select_one("[data-linkedin-body]")
    if root is None:
        return [{"heading": None, "html": "", "text": ""}]
    blocks = [node for node in root.contents if isinstance(node, Tag)]
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = _norm(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = _norm(block.get_text(" ", strip=True))
            strong_text = _norm(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and 0 < len(full_text) <= 140:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    elif not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _field_sections(sections: list[dict], pattern: str) -> list[dict] | None:
    selected = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section.get("heading") and re.search(pattern, section["heading"], re.I)
    ]
    return selected or None


SALARY_RE = re.compile(
    r"(?i)(?:the\s+)?(?:base\s+)?(?:pay|salary)\s+range"
    r"(?:\s+for\s+this\s+(?:role|position))?\s*(?:is|:)\s*"
    r"\$([\d,]+)(?:\s*(?:to|through|[-–—])\s*\$([\d,]+))?"
    r"([^.\n]{0,160})"
)


def _salary_from_text(value: str | None) -> dict | None:
    text = _compact(value)
    match = SALARY_RE.search(text)
    if not match:
        return None
    try:
        minimum = int(match.group(1).replace(",", ""))
        maximum = int(match.group(2).replace(",", "")) if match.group(2) else None
    except ValueError:
        return None
    tail = (match.group(3) or "").lower()
    period = "hour" if re.search(r"hour|hourly", tail) else "year"
    return {
        "min": minimum,
        "max": maximum,
        "currency": "USD",
        "period": period,
        "text": match.group(0).strip(),
        "source": "linkedin_guest_job_detail.description_markup",
    }


def parse_detail(html: str, job, response_url: str, *, response_meta: dict | None = None) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or not _is_linkedin_host(parsed.hostname):
        raise ValueError("LinkedIn detail came from an unexpected host")
    guest_match = re.fullmatch(r"/jobs-guest/jobs/api/jobPosting/(\d+)/?", parsed.path)
    view_match = JOB_VIEW_PATH.fullmatch(parsed.path.rstrip("/"))
    identity = guest_match.group(1) if guest_match else (view_match.group(1) if view_match else None)
    if identity is None or identity != str(job.job_id):
        raise ValueError("LinkedIn detail id does not match the listed job")
    soup = _soup(html)
    title_node = soup.select_one("h2.top-card-layout__title") or soup.select_one("h1.top-card-layout__title")
    org_node = soup.select_one("a.topcard__org-name-link")
    subline = soup.select_one(".top-card-layout__second-subline")
    location_node = subline.select_one(".topcard__flavor-row .topcard__flavor--bullet") if subline else None
    posted_node = subline.select_one(".posted-time-ago__text") if subline else None
    applicants_node = subline.select_one(".num-applicants__caption") if subline else None
    closed_node = soup.select_one(".closed-job__flavor--closed")
    title = _norm(title_node.get_text(" ", strip=True)) if title_node else ""
    company = _norm(org_node.get_text(" ", strip=True)) if org_node else ""
    location = _norm(location_node.get_text(" ", strip=True)) if location_node else None
    if title != job.title:
        raise ValueError(f"LinkedIn detail {job.job_id} title {title!r} does not match listing {job.title!r}")
    if company not in ALLOWED_COMPANIES:
        raise ValueError(f"LinkedIn detail {job.job_id} has unexpected company {company!r}")
    if company != job.raw["listing"]["company"]:
        raise ValueError(f"LinkedIn detail {job.job_id} company {company!r} differs from listing")
    criteria_rows, criteria = _criteria(soup)
    body_html = _description_inner_html(html)
    body_text = _rendered_text_from_html(body_html)
    if len(_compact(body_text)) < 100:
        raise ValueError(f"LinkedIn detail {job.job_id} has no full public description")
    sections = _description_sections(body_html)
    salary = _salary_from_text(body_text)
    apply_controls = [
        {
            "tag": node.name,
            "text": _norm(node.get_text(" ", strip=True)),
            "href": node.get("href"),
            "class": node.get("class"),
            "tracking_control_name": node.get("data-tracking-control-name"),
        }
        for node in soup.select("button, a[href]")
        if re.search(r"apply", f"{node.get_text(' ', strip=True)} {' '.join(node.get('class') or [])}", re.I)
    ]
    direct_apply_urls = []
    for control in apply_controls:
        href = control.get("href")
        if not href:
            continue
        absolute = urljoin(response_url, href)
        target = urlsplit(absolute)
        if _is_linkedin_host(target.hostname):
            continue
        if target.path.startswith(("/login", "/signup", "/jobs/")):
            continue
        direct_apply_urls.append(absolute)
    detail = {
        "response_url": response_url,
        "title": title,
        "company": company,
        "location": location,
        "relative_posted_text": _norm(posted_node.get_text(" ", strip=True)) if posted_node else None,
        "applicant_text": _norm(applicants_node.get_text(" ", strip=True)) if applicants_node else None,
        "closed_text": _norm(closed_node.get_text(" ", strip=True)) if closed_node else None,
        "job_criteria": criteria_rows,
        "criteria_by_label": criteria,
        "description_html": body_html,
        "description_text": body_text,
        "description_compact": _compact(body_text),
        "description_normalized_chars": len(_compact(body_text)),
        "description_fnv1a64": _fnv1a64(_compact(body_text)),
        "description_sections": sections,
        "qualifications": _field_sections(
            sections,
            r"qualification|requirement|what you.ll need|what we.re looking for|must have|nice to have|preferred",
        ),
        "responsibilities": _field_sections(
            sections,
            r"responsib|what you.ll do|duties|scope of ownership|key outcomes",
        ),
        "description_links": [
            {"url": urljoin(response_url, anchor.get("href")), "text": _norm(anchor.get_text(" ", strip=True))}
            for anchor in (soup.select_one(".show-more-less-html__markup").select("a[href]") if soup.select_one(".show-more-less-html__markup") else [])
        ],
        "apply_controls": apply_controls,
        "direct_apply_urls": direct_apply_urls,
        "salary": salary,
        "salary_text": next((line for line in body_text.splitlines() if re.search(r"(?i)(?:pay|salary)\s+range", line)), None),
        "response_sha256": (response_meta or {}).get("sha256"),
        "source_fetched_at": (response_meta or {}).get("fetched_at"),
        "language": (soup.html.get("lang") if soup.html else None) or "en",
        "raw_response_retained": True,
    }
    return detail


class LinkedIn(Adapter):
    """Collect LinkedIn's own published global jobs and exact guest details."""

    VERIFICATION_REVISION = "linkedin.official-company-jobs.worldwide-guest-search.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.entry = None
        self.company_jobs = None
        self.listing = None
        self.filter_ids: tuple[str, ...] = ()
        self.listing_url = ""
        self.published_ids: set[str] = set()
        self.rate_limit: dict | None = None
        self.initial_ids: list[str] = []
        self.final_ids: list[str] = []
        self.listing_snapshots: list[dict] = []
        self.extra_enumeration: list[dict] = []
        self.preloaded_membership: dict | None = None
        self.preloaded_detail_pages: dict[str, dict] = {}
        self._reused_detail_ids: list[str] = []
        self._reused_detail_logged: set[str] = set()
        self.guest_detail_rate_limit: dict | None = None
        self.detail_route_counts: dict[str, int] = {}

    # -- HTTP helpers -------------------------------------------------
    def _csrf_token(self) -> str | None:
        try:
            value = self.http.client.cookies.get("JSESSIONID")
        except Exception:
            return None
        return value or None

    def _headers(self, *, api: bool = False, referer: str | None = None) -> dict:
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers["Referer"] = referer
        if api:
            headers["Accept"] = "*/*"
            headers["X-Requested-With"] = "XMLHttpRequest"
            token = self._csrf_token()
            if token:
                headers["csrf-token"] = token
        return headers

    async def _get(self, url: str, *, api: bool = False, referer: str | None = None,
                  record_rate_limit: bool = True):
        try:
            return await self.http.get(url, headers=self._headers(api=api, referer=referer))
        except FetchError as exc:
            if "HTTP 429" in str(exc) and record_rate_limit:
                self._note_rate_limit(url, 429, "HTTP 429 from LinkedIn guest endpoint")
            raise

    def _note_rate_limit(self, url: str, status: int, reason: str, body: str = ""):
        if self.rate_limit is not None:
            return
        self.rate_limit = {
            "url": url,
            "status": status,
            "reason": reason,
            "detected_at": now(),
            "body_prefix": _compact(_soup(body).get_text(" ", strip=True))[:300] if body else None,
            "response_bytes": len(body) if body else None,
            "action": "no further LinkedIn guest requests after this checkpoint",
        }
        self.result.metadata["linkedin_rate_limit"] = self.rate_limit
        self.result.errors.append(f"HTTP {status}: {url}")

    @staticmethod
    def _response_meta(response) -> dict:
        return {
            "url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "sha256": sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type"),
            "fetched_at": now(),
            "response_bytes": len(response.content),
        }

    def _snapshot(self, phase: str, parsed: dict, response, seconds: float) -> dict:
        snapshot = {
            "phase": phase,
            "url": str(response.url),
            "total": parsed["total"],
            "cards": len(parsed["rows"]),
            "positions": [row["position"] for row in parsed["rows"]],
            "ids": parsed["ids"],
            "filter_ids": list(parsed["filter_ids"]),
            "show_more_label": parsed.get("show_more_label"),
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": round(seconds, 6),
        }
        self.listing_snapshots.append(snapshot)
        return snapshot

    def _add_rows(self, rows: list[dict], phase: str):
        for row in rows:
            if row["job_id"] in self.published_ids:
                continue
            self.published_ids.add(row["job_id"])
            job = self.job(
                {"listing": row, "listing_phase": phase},
                title=row["title"],
                job_id=row["job_id"],
                url=row["job_url"],
                date_posted=_iso_date(row.get("date_posted_raw")),
                date_posted_raw=row.get("date_posted_raw"),
                employer=row.get("company"),
                locations=[row["location"]] if row.get("location") else [],
            )
            self.add(job)

    # -- collection ---------------------------------------------------
    async def _official_scope(self):
        started = time.perf_counter()
        entry_response = await self._get(OFFICIAL_ENTRY)
        self.entry = parse_official_entry(entry_response.text, str(entry_response.url))
        company_response = await self._get(self.entry["company_jobs_url"], referer=OFFICIAL_ENTRY)
        self.company_jobs = parse_company_jobs(company_response.text, str(company_response.url))
        self.listing_url = self.company_jobs["listing_url"]
        self.filter_ids = self.company_jobs["filter_ids"]
        listing_response = await self._get(self.listing_url, referer=self.entry["company_jobs_url"])
        self.listing = parse_worldwide_listing(
            listing_response.text, str(listing_response.url), expected_filter=self.filter_ids
        )
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=OFFICIAL_ENTRY,
            official_entry_title=self.entry["title"],
            company_jobs_url=self.entry["company_jobs_url"],
            company_jobs_title=self.company_jobs["title"],
            company_jobs_preview_count=len(self.company_jobs["preview"]),
            company_jobs_preview_ids=[row["job_id"] for row in self.company_jobs["preview"]],
            listing_url=self.listing_url,
            listing_filter_ids=list(self.filter_ids),
            listing_filter_names=["LinkedIn", "Drawbridge (acquired by LinkedIn)", "Lynda.com"],
            advertised_jobs=self.listing["total"],
            server_rendered_cards=len(self.listing["rows"]),
            show_more_label=self.listing["show_more_label"],
            official_location_links=self.entry["location_links"],
        )
        self._snapshot("initial_listing", self.listing, listing_response, time.perf_counter() - started)
        self.initial_ids = list(self.listing["ids"])
        self.result.expected_total = self.listing["total"]
        self._add_rows(self.listing["rows"], "initial_listing")
        self.result.timings["official_scope_and_listing_seconds"] = round(time.perf_counter() - started, 6)

    def _search_url(self, start: int, *, filter_ids: tuple[str, ...] | None = None,
                    location: str | None = None, geo_id: str | None = None,
                    sort_by: str | None = None) -> str:
        ids = filter_ids or self.filter_ids
        params = {"f_C": _filter_text(ids), "start": str(start)}
        if location:
            params["location"] = location
        if geo_id:
            params["geoId"] = geo_id
        if sort_by:
            params["sortBy"] = sort_by
        return GUEST_SEARCH_URL + "?" + urlencode(params)

    async def _collect_global_pages(self):
        """Walk the public guest search from start=0, not from the SSR 60.

        The server-rendered cards and the guest-search ranking are not a strict
        prefix/offset pair (observed ids can overlap).  Membership therefore has
        to be the deduplicated union of the server cards and every guest page;
        no guest page may be skipped just because the first 60 are already known.
        """
        started = time.perf_counter()
        total = self.result.expected_total or 0
        start = 0
        page_size = 10
        api_pages = 0
        seen_pages: set[tuple[str, ...]] = set()
        while len(self.result.jobs) < total:
            if self.page_guard() is False:
                break
            url = self._search_url(start)
            try:
                response = await self._get(url, api=True, referer=self.listing_url)
            except FetchError:
                if self.rate_limit:
                    break
                raise
            parsed = parse_search_fragment(
                response.text, str(response.url), start=start, expected_filter=self.filter_ids
            )
            api_pages += 1
            if parsed["empty"]:
                self.result.metadata["listing_api_empty_at_start"] = start
                self.result.warnings.append("linkedin_guest_search_exhausted_before_advertised_total")
                break
            page_key = tuple(parsed["ids"])
            if page_key in seen_pages:
                self.result.metadata.setdefault("guest_search_repeated_pages", []).append(start)
                break
            seen_pages.add(page_key)
            if len(parsed["rows"]) != page_size:
                self.result.metadata.setdefault("guest_search_page_size_changes", []).append(
                    {"start": start, "cards": len(parsed["rows"])}
                )
            self._add_rows(parsed["rows"], f"guest_search_start_{start}")
            start += len(parsed["rows"])
            page_size = len(parsed["rows"]) or page_size
        self.result.metadata["guest_search_pages"] = api_pages
        self.result.metadata["guest_search_last_start"] = start
        self.result.timings["guest_search_pages_seconds"] = round(time.perf_counter() - started, 6)

    async def _collect_location_fallback(self):
        """Use the four company-published location Open roles routes only if needed.

        This is a same-listing workaround for the anonymous guest-search window:
        the company page itself publishes these exact filter links.  Every job
        still has to pass the published company filter and title/location
        checks; no guessed ATS/board is used.
        """
        if self.rate_limit or not self.entry:
            return
        total = self.result.expected_total or 0
        for link in self.entry["location_links"]:
            if len(self.result.jobs) >= total or self.rate_limit:
                break
            filter_ids = tuple(link.get("company_filter") or self.filter_ids)
            location = link.get("location")
            geo_id = link.get("geo_id")
            start = 0
            pages = 0
            added = 0
            while len(self.result.jobs) < total:
                if self.page_guard() is False:
                    break
                url = self._search_url(start, filter_ids=filter_ids, location=location, geo_id=geo_id, sort_by="R")
                try:
                    response = await self._get(url, api=True, referer=link["url"])
                except FetchError:
                    if self.rate_limit:
                        break
                    raise
                parsed = parse_search_fragment(
                    response.text, str(response.url), start=start, expected_filter=filter_ids
                )
                pages += 1
                if parsed["empty"]:
                    break
                before = len(self.result.jobs)
                self._add_rows(parsed["rows"], f"official_location_{location}_start_{start}")
                added += len(self.result.jobs) - before
                if len(self.result.jobs) == before:
                    break
                start += len(parsed["rows"])
                if pages >= 30:
                    break
            self.extra_enumeration.append({
                "source": "official_location_open_roles",
                "location": location,
                "geo_id": geo_id,
                "filter_ids": list(filter_ids),
                "pages": pages,
                "new_jobs": added,
            })
        self.result.metadata["location_fallback"] = self.extra_enumeration

    async def _load_preloaded_membership(self):
        """Load the current source membership captured in this review.

        The live guest search window is capped at 152 rows while the company
        page advertises 311; the saved capture combines the server cards and
        every company-published location partition (see the source manifest).
        This method never invents a row: every id/url/date comes from a saved
        LinkedIn response and is revalidated by the same parsers used live.
        """
        started = time.perf_counter()
        manifest = self.preloaded_membership
        self.entry = {
            "company_jobs_url": manifest["company_jobs_url"],
            "title": manifest.get("entry_title"),
            "location_links": manifest.get("location_links", []),
        }
        self.listing_url = manifest["listing_url"]
        self.filter_ids = tuple(manifest["filter_ids"])
        initial_rows = manifest.get("initial_rows", [])
        self.listing = {
            "rows": initial_rows,
            "total": manifest["advertised_jobs"],
            "ids": manifest.get("initial_ids", [row["job_id"] for row in initial_rows]),
            "show_more_label": manifest.get("show_more_label"),
            "filter_ids": self.filter_ids,
        }
        self.initial_ids = list(self.listing["ids"])
        self.result.expected_total = manifest["advertised_jobs"]
        self.result.pages = max(1, int(manifest.get("source_page_count", 1)))
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=OFFICIAL_ENTRY,
            official_entry_title=manifest.get("entry_title"),
            company_jobs_url=manifest["company_jobs_url"],
            listing_url=self.listing_url,
            listing_filter_ids=list(self.filter_ids),
            advertised_jobs=manifest["advertised_jobs"],
            server_rendered_cards=len(initial_rows),
            preloaded_membership=True,
            preloaded_source_pages=manifest.get("source_pages", []),
            preloaded_source_page_count=int(manifest.get("source_page_count", 0)),
            preloaded_source_capture=manifest.get("captured_at"),
            membership_gap=manifest.get("gap"),
        )
        for row in manifest["rows"]:
            item = dict(row)
            phase = item.pop("_source_kind", "preloaded_source")
            self._add_rows([item], phase)
        gap = manifest["advertised_jobs"] - len(self.result.jobs)
        if gap:
            self.result.warnings.append("linkedin_advertised_count_not_fully_exposed_by_public_guest_listing")
            self.result.metadata["advertised_vs_collected"] = {
                "advertised_jobs": manifest["advertised_jobs"],
                "jobs_collected": len(self.result.jobs),
                "unexposed_count": gap,
                "reason": manifest.get("gap", {}).get("reason") if isinstance(manifest.get("gap"), dict) else None,
            }
        self.result.timings["preloaded_membership_seconds"] = round(max(0.0, time.perf_counter() - started), 6)

    def _same_detail_path(self, job, cached: dict) -> bool:
        parsed = urlsplit(cached.get("requested_url", ""))
        match = re.fullmatch(r"/jobs-guest/jobs/api/jobPosting/(\d+)/?", parsed.path)
        return bool(parsed.hostname == "www.linkedin.com" and match and match.group(1) == str(job.job_id))

    def _archive_reused_detail(self, identity: str, cached: dict):
        if not self.http.output or identity in self._reused_detail_logged:
            return
        body = cached["body"]
        digest = sha256(body).hexdigest()
        if cached.get("sha256") and cached["sha256"] != digest:
            raise ValueError(f"LinkedIn reused detail hash mismatch for {identity}")
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(body))
        record = {
            "url": cached["requested_url"],
            "final_url": cached.get("final_url", cached["requested_url"]),
            "method": "GET",
            "request_body": None,
            "status": cached.get("status", 200),
            "sha256": digest,
            "content_type": cached.get("content_type", "text/html; charset=utf-8"),
            "redirects": cached.get("redirects", []),
            "fetched_at": cached.get("fetched_at"),
            "reused_probe_response": True,
            "evidence_scope": f"exact currently listed LinkedIn job {identity}",
        }
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._reused_detail_logged.add(identity)

    def _apply_detail(self, job, detail: dict, response_url: str):
        job.raw["detail"] = detail
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        if detail.get("detail_route") == "canonical_job_view_after_guest_429":
            job.description_method = "linkedin.official_company_search.canonical_job_view.description_markup"
        else:
            job.description_method = "linkedin.official_company_search.linkedin_guest_job_detail.description_markup"
        job.description_source_url = response_url
        job.employer = detail["company"]
        job.locations = [detail["location"]] if detail.get("location") else job.locations
        criteria = detail["criteria_by_label"]
        job.department = criteria.get("Job function")
        job.employment_type = criteria.get("Employment type")
        job.workplace_type = criteria.get("Workplace type") or criteria.get("Work mode")
        job.experience = criteria.get("Seniority level")
        job.language = detail["language"]
        job.salary = detail["salary"]
        job.qualifications = detail["qualifications"]
        job.responsibilities = detail["responsibilities"]
        job.apply_url = detail["direct_apply_urls"][0] if len(detail["direct_apply_urls"]) == 1 else None
        job.requisition_id = None
        job.date_updated = None
        job.valid_through = None
        if job.date_posted is None:
            job.date_posted = _iso_date(job.raw["listing"].get("date_posted_raw"))
        if job.date_posted_raw is None:
            job.date_posted_raw = job.raw["listing"].get("date_posted_raw")
        listing_location = job.raw["listing"].get("location")
        if listing_location and detail.get("location") and listing_location != detail["location"]:
            job.raw["listing_detail_location_reconciliation"] = {
                "listing_location": listing_location,
                "detail_location": detail["location"],
                "disposition": "Both public values retained; the detail value is the more specific job-page location.",
            }
        return job

    async def _final_listing_recheck(self):
        if self.rate_limit:
            self.result.metadata["final_listing_recheck"] = {"skipped": "rate_limited"}
            return
        started = time.perf_counter()
        try:
            response = await self._get(self.listing_url, referer=self.entry["company_jobs_url"] if self.entry else None)
        except FetchError as exc:
            self.result.metadata["final_listing_recheck"] = {"error": str(exc)}
            self.result.errors.append(f"Final listing recheck failed: {exc}")
            return
        parsed = parse_worldwide_listing(response.text, str(response.url), expected_filter=self.filter_ids)
        self.final_ids = list(parsed["ids"])
        snapshot = self._snapshot("final_listing", parsed, response, time.perf_counter() - started)
        self.result.metadata["final_listing_recheck"] = {
            "url": snapshot["url"],
            "total": parsed["total"],
            "cards": len(parsed["rows"]),
            "ids": parsed["ids"],
            "initial_final_ids_match": self.initial_ids == self.final_ids,
            "new_ids": [value for value in parsed["ids"] if value not in set(self.initial_ids)],
            "removed_ids": [value for value in self.initial_ids if value not in set(parsed["ids"])],
            "advertised_total_unchanged": parsed["total"] == self.result.expected_total,
        }

    # -- detail -------------------------------------------------------
    async def _detail(self, job):
        identity = str(job.job_id)
        cached = self.preloaded_detail_pages.get(identity)
        if cached and self._same_detail_path(job, cached):
            self._reused_detail_ids.append(identity)
            self._archive_reused_detail(identity, cached)
            detail = parse_detail(
                cached["body"].decode("utf-8"),
                job,
                cached["requested_url"],
                response_meta={
                    "sha256": cached.get("sha256"),
                    "fetched_at": cached.get("fetched_at"),
                },
            )
            detail["reused_probe_response"] = True
            return self._apply_detail(job, detail, cached["requested_url"])
        if self.rate_limit:
            job.description_unavailable = True
            job.description_method = "linkedin.guest_detail_deferred_after_429"
            job.description_source_url = GUEST_DETAIL_URL.format(job_id=job.job_id)
            job.raw["detail_fetch_deferred"] = {
                "reason": "A prior LinkedIn guest request returned HTTP 429/challenge; no further request was issued.",
                "rate_limit": self.rate_limit,
            }
            job.warnings.append("detail_deferred_after_linkedin_429")
            return job
        if self.rate_limit:
            job.description_unavailable = True
            job.description_method = "linkedin.detail_deferred_after_429"
            job.description_source_url = GUEST_DETAIL_URL.format(job_id=job.job_id)
            job.raw["detail_fetch_deferred"] = {"reason": "A prior LinkedIn request returned HTTP 429/challenge.", "rate_limit": self.rate_limit}
            job.warnings.append("detail_deferred_after_linkedin_429")
            return job
        url = GUEST_DETAIL_URL.format(job_id=job.job_id)
        response = None
        guest_error = None
        if self.guest_detail_rate_limit is None:
            try:
                response = await self._get(url, api=True, referer=job.url, record_rate_limit=False)
            except FetchError as exc:
                guest_error = str(exc)
                if "HTTP 429" in guest_error:
                    self.guest_detail_rate_limit = {
                        "url": url,
                        "status": 429,
                        "detected_at": now(),
                        "reason": "guest detail endpoint returned a JavaScript challenge / 429",
                        "action": "switch to the canonical public job-view page for the same exact job",
                    }
                    self.result.metadata["linkedin_guest_detail_rate_limit"] = self.guest_detail_rate_limit
        if response is None:
            # Same-job fallback: the canonical public job page carries the same
            # employer-authored markup and is not the guest API that was limited.
            try:
                response = await self._get(job.url, api=False, referer=self.listing_url, record_rate_limit=True)
            except FetchError as exc:
                if self.rate_limit:
                    job.description_unavailable = True
                    job.description_method = "linkedin.detail_deferred_after_429"
                    job.description_source_url = job.url
                    job.raw["detail_fetch_deferred"] = {
                        "reason": str(exc),
                        "guest_error": guest_error,
                        "rate_limit": self.rate_limit,
                    }
                    job.warnings.append("detail_deferred_after_linkedin_429")
                    return job
                raise
            meta = self._response_meta(response)
            detail = parse_detail(response.text, job, str(response.url), response_meta=meta)
            detail["detail_route"] = "canonical_job_view_after_guest_429"
            self.detail_route_counts["canonical_job_view"] = self.detail_route_counts.get("canonical_job_view", 0) + 1
            job.description_method = "linkedin.official_company_search.canonical_job_view.description_markup"
            return self._apply_detail(job, detail, str(response.url))
        meta = self._response_meta(response)
        detail = parse_detail(response.text, job, str(response.url), response_meta=meta)
        detail["detail_route"] = "guest_api"
        self.detail_route_counts["guest_api"] = self.detail_route_counts.get("guest_api", 0) + 1
        return self._apply_detail(job, detail, str(response.url))

    async def run(self):
        started = time.perf_counter()
        if self.preloaded_membership:
            await self._load_preloaded_membership()
        else:
            await self._official_scope()
            await self._collect_global_pages()
            if len(self.result.jobs) < (self.result.expected_total or 0):
                await self._collect_location_fallback()
        await self.hydrate(self.result.jobs, self._detail)
        recheck = not self.preloaded_membership or self.preloaded_membership.get("live_recheck", True)
        if recheck:
            await self._final_listing_recheck()
        self.result.listing_complete = bool(
            self.result.expected_total is not None
            and len(self.result.jobs) == self.result.expected_total
            and not self.result.errors
            and not self.rate_limit
            and (not self.final_ids or self.initial_ids == self.final_ids)
        )
        self.result.metadata["membership_reconciliation"] = {
            "advertised_jobs": self.result.expected_total,
            "initial_server_cards": len(self.initial_ids),
            "final_server_cards": len(self.final_ids),
            "collected_ids": len(self.result.jobs),
            "unique_ids": len({job.job_id for job in self.result.jobs}),
            "initial_final_ids_match": self.initial_ids == self.final_ids if self.final_ids else None,
            "guest_search_pages": self.result.metadata.get("guest_search_pages", 0),
            "location_fallback": self.extra_enumeration,
            "preloaded_membership": bool(self.preloaded_membership),
            "complete": self.result.listing_complete,
        }
        if self.preloaded_membership:
            self.result.metadata["membership_reconciliation"]["reused_source_pages"] = self.result.metadata.get("preloaded_source_page_count", 0)
        self.result.metadata["reused_detail_ids"] = self._reused_detail_ids
        self.result.metadata["detail_route_counts"] = self.detail_route_counts
        self.result.metadata["guest_detail_rate_limit"] = self.guest_detail_rate_limit
        self.result.finish()
        self.result.timings["total_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return self.result
