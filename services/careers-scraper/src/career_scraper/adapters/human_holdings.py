"""Human Holdings' official careers page (reviewed 2026-11-07).

The ledger URL ``https://www.athuman.com/recruit/`` is the parent company's
recruitment-information page.  It publishes culture/interview articles and a
"Group Company" directory linking the separate group companies' own recruitment
sites.  It does **not** publish a Human-Holdings-scoped job listing: the 総合
card for Human Holdings itself only links to the corporate inquiry form.

The group companies are separate legal entities.  A parent page that links them
is not proof that their postings are Human Holdings postings; the project
contract explicitly excludes a parent-wide board from requested-company scope.
This adapter therefore records the absence of a company-scoped listing instead
of enumerating the group companies, and it never falls back to a guessed ATS
slug, aggregator or parent/group board.

If the official page later publishes a real job listing (job cards, job links,
``JobPosting`` JSON-LD, ATS embed, job filters or pagination), the adapter fails
with ``unreviewed_careers_listing_detected`` so the new journey is inspected by
a human before membership is collected.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.athuman.com/recruit/"
EXPECTED_TITLE = "採用情報 | ヒューマンホールディングス株式会社"
EXPECTED_H1_JA = "ヒューマングループ採用情報"
EXPECTED_H1_EN = "Recruit"
GROUP_SECTION_TITLE = "グループ会社の採用情報"
GROUP_SECTION_TITLE_EN = "Group Company"
GROUP_SECTION_LEAD = "グループ各社ごとに採用状況や応募条件などが異なります。"
PARENT_CONTACT_HREF = "https://www.athuman.com/contact/consent/?summary=recruit"

# The exact group companies published in the page's "グループ会社の採用情報"
# section.  Human Academy appears in two business blocks with two different
# links; both are preserved.  These are scope evidence only and are not scraped.
GROUP_COMPANIES: dict[str, tuple[str, ...]] = {
    "ヒューマンアカデミー株式会社": (
        "https://manabu.athuman.com/",
        "https://manabu.athuman.com/information/",
    ),
    "ヒューマンリソシア株式会社": ("https://recruit.resocia.jp/",),
    "ヒューマンライフケア株式会社": ("https://jobs.human-lifecare.jp/",),
    "ヒューマンスターチャイルド株式会社": ("https://www.starchild.jp/recruit/",),
    "ダッシングディバインターナショナル株式会社": ("https://www.dashingdiva.co.jp/recruit/",),
    "ヒューマンプランニング株式会社": ("https://www.evessa.com/recruit/",),
}
EXPECTED_GROUP_LINKS = frozenset(
    url for urls in GROUP_COMPANIES.values() for url in urls
)

# Hosts that would indicate a real external job board/ATS embed.  None appears
# on the inspected page; the list exists so a future change is detected.
ATS_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io|"
    r"jobs\.lever\.co|lever\.co|"
    r"(?:[a-z0-9-]+\.)?myworkdayjobs\.com|workday\.com|"
    r"ashbyhq\.com|smartrecruiters\.com|"
    r"keka\.com|darwinbox\.com|darwinbox\.in|"
    r"zohorecruit\.com|zohorecruit\.in|"
    r"turbohire\.co|recruitee\.com|workable\.com|bamboohr\.com|freshteam\.com|"
    r"hirepro\.in|recruiterflow\.com|jazz\.co|applytojob\.com|jobvite\.com|"
    r"icims\.com|taleo\.net|oraclecloud\.com|successfactors\.com|"
    r"phenompeople\.com|eightfold\.ai|kula\.ai|hrone\.cloud|"
    r"official\.career|snar\.jp|mynavi\.jp|good-for-job\.jp"
    r")$"
)
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:job|jobs|job-|position|positions|opening|openings|"
    r"vacanc(?:y|ies)|apply)(?:/|$|[?#])"
)
CULTURE_PATH = re.compile(r"^/recruit/(?:selfing-talk|career-interview|para-talk)/?$", re.I)
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b|次へ|前へ|もっと見る|次ページ|前ページ")
JOB_FILTER_TEXT = re.compile(r"職種|エリア|勤務地|求人|キーワード|occupation|location|keyword", re.I)
JOB_FILTER_FIELD = re.compile(r"occupation|category|area|pref|location|keyword|job|position|search", re.I)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def _count_job_postings(value) -> int:
    if isinstance(value, dict):
        types = value.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            return 1
        return sum(_count_job_postings(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_job_postings(item) for item in value)
    return 0


def job_posting_schema_count(soup: BeautifulSoup) -> int:
    total = 0
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            total += _count_job_postings(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return total


def _group_section(soup: BeautifulSoup):
    for section in soup.find_all("section"):
        headings = section.find_all(["h2", "h3"], limit=3)
        heading_text = " ".join(normalize_text(h.get_text(" ", strip=True)) for h in headings)
        if GROUP_SECTION_TITLE in heading_text or GROUP_SECTION_TITLE_EN in heading_text:
            return section
    return None


def _parse_group_links(group_section, response_url: str) -> tuple[list[dict], list[dict]]:
    links, parent_contacts = [], []
    seen = set()
    for block in group_section.select("section.p-recruit-group-company__section"):
        title_node = block.select_one("h3.p-recruit-group-company__title")
        business = normalize_text(title_node.get_text(" ", strip=True)) if title_node else ""
        for anchor in block.select("a.p-recruit-group-company__link[href]"):
            href = (anchor.get("href") or "").strip()
            absolute = _absolute(response_url, href)
            name_node = anchor.select_one("p.p-recruit-group-company__name")
            name = normalize_text(name_node.get_text(" ", strip=True)) if name_node else ""
            title_attr = normalize_text(anchor.get("title", ""))
            if absolute == PARENT_CONTACT_HREF or name == "ヒューマンホールディングス株式会社":
                # The 総合 block links Human Holdings itself to the corporate
                # inquiry form; it is not a group-company recruitment page.
                parent_contacts.append({
                    "business": business,
                    "company": name,
                    "title_attribute": title_attr,
                    "href": href,
                    "url": absolute,
                })
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append({
                "business": business,
                "company": name,
                "title_attribute": title_attr,
                "href": href,
                "url": absolute,
            })
    return links, parent_contacts


def _job_cards(soup: BeautifulSoup) -> list[dict]:
    selectors = (
        '[class*="job-card"]', '[class*="job-list"]', '[class*="job-item"]',
        '[class*="job-offer"]', '[class*="position-card"]', '[class*="vacancy"]',
        '[class*="opening"]', '[id*="job-list"]', '[id*="job-card"]',
    )
    cards, seen = [], set()
    for selector in selectors:
        for node in soup.select(selector):
            anchor = node.find("a", href=True)
            if anchor is None:
                continue
            text = normalize_text(node.get_text(" ", strip=True))
            key = (anchor.get("href"), text[:120])
            if key in seen:
                continue
            seen.add(key)
            cards.append({"href": anchor.get("href"), "text": text[:300]})
    return cards


def _company_job_links(soup: BeautifulSoup, response_url: str) -> list[dict]:
    links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _absolute(response_url, href)
        if _host(absolute) not in {"www.athuman.com", "athuman.com"}:
            continue
        path = urlsplit(absolute).path
        if CULTURE_PATH.match(path):
            continue
        if JOB_PATH.search(path):
            links.append({
                "text": normalize_text(anchor.get_text(" ", strip=True)),
                "href": href,
                "url": absolute,
            })
    return links


def _ats_embeds(soup: BeautifulSoup, response_url: str) -> list[dict]:
    embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    return embeds


def _job_filters(soup: BeautifulSoup) -> list[dict]:
    filters = []
    for form in soup.select("form"):
        action = (form.get("action") or "").strip()
        text = normalize_text(form.get_text(" ", strip=True))
        fields = []
        for field in form.select("input[name], select[name], textarea[name]"):
            fields.append(field.get("name", ""))
        if not fields:
            continue
        action_is_job = bool(re.search(r"(?i)(?:job|career|recruit|position)", action))
        text_is_job = bool(JOB_FILTER_TEXT.search(text))
        fields_are_job = any(JOB_FILTER_FIELD.search(name) for name in fields)
        if (action_is_job or text_is_job) and fields_are_job:
            filters.append({
                "action": action,
                "text": text[:200],
                "fields": fields,
            })
    return filters


def _pagination_controls(soup: BeautifulSoup) -> list[str]:
    controls = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and PAGINATION_TEXT.search(text):
            controls.append(text)
    return controls


def parse_official_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse the official Human Holdings careers page.

    Raises ``ValueError`` when the response is no longer the inspected page, so
    a redirect/redesign is reported instead of being treated as an empty
    listing.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.athuman.com", "athuman.com"}
            or parts.path.rstrip("/") != "/recruit"):
        raise ValueError(f"Human Holdings careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"Human Holdings careers page title changed: {title!r}")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    h1_nodes = [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if not any(EXPECTED_H1_JA in value for value in h1_nodes):
        raise ValueError(f"Human Holdings careers page h1 changed: {h1_nodes!r}")

    group_section = _group_section(soup)
    if group_section is None:
        raise ValueError("Human Holdings group-company recruitment section is missing")
    if GROUP_SECTION_LEAD not in body_text:
        raise ValueError("Human Holdings group-company recruitment lead text changed")

    group_links, parent_contacts = _parse_group_links(group_section, response_url)
    group_name_set = {item["company"] for item in group_links if item["company"]}
    group_names = sorted(group_name_set)
    group_urls = {item["url"] for item in group_links}
    expected_names = set(GROUP_COMPANIES)
    unexpected_group_names = sorted(group_name_set - expected_names)
    missing_group_names = sorted(expected_names - group_name_set)
    unexpected_group_urls = sorted(group_urls - EXPECTED_GROUP_LINKS)
    missing_group_urls = sorted(EXPECTED_GROUP_LINKS - group_urls)

    cards = _job_cards(soup)
    company_job_links = _company_job_links(soup, response_url)
    ats_embeds = _ats_embeds(soup, response_url)
    schema_count = job_posting_schema_count(soup)
    filters = _job_filters(soup)
    pagination = _pagination_controls(soup)

    listing_signals = {
        "job_cards": cards,
        "company_job_links": company_job_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": schema_count,
        "job_filters": filters,
    }
    has_listing_signal = bool(
        cards or company_job_links or ats_embeds or schema_count or filters
    )

    return {
        "response_url": response_url,
        "title": title,
        "h1": h1_nodes,
        "headings": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "group_section_found": True,
        "group_company_links": group_links,
        "group_company_names": group_names,
        "group_company_urls": sorted(group_urls),
        "unexpected_group_names": unexpected_group_names,
        "missing_group_names": missing_group_names,
        "unexpected_group_urls": unexpected_group_urls,
        "missing_group_urls": missing_group_urls,
        "parent_contact_links": parent_contacts,
        "job_cards": cards,
        "company_job_links": company_job_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": schema_count,
        "job_filters": filters,
        "pagination_controls": pagination,
        "listing_signals": listing_signals,
        "has_listing_signal": has_listing_signal,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
    }


def _evidence(parsed: dict) -> dict:
    """Stable evidence used to compare the initial and final recheck."""
    return {
        "title": parsed["title"],
        "h1": parsed["h1"],
        "group_company_links": [
            {"business": item["business"], "company": item["company"], "url": item["url"]}
            for item in parsed["group_company_links"]
        ],
        "parent_contact_links": [
            {"company": item["company"], "url": item["url"]}
            for item in parsed["parent_contact_links"]
        ],
        "has_listing_signal": parsed["has_listing_signal"],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
    }


def _entry_record(response) -> dict:
    return {
        "requested_url": OFFICIAL_ENTRY,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "html_bytes": len(response.content),
        "html_sha256": sha256(response.content).hexdigest(),
        "redirects": [
            {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
            for item in response.history
        ],
        "fetched_at": now(),
    }


class HumanHoldings(Adapter):
    """Read only Human Holdings' official careers page and record its listing state."""

    VERIFICATION_REVISION = "human-holdings.official-group-hub-no-parent-listing.1"

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_authority": "none: the official page publishes no Human-Holdings-scoped job listing",
            "detail_method": "none: no Human Holdings role or detail route is published",
            "request_scope": (
                "Official Human Holdings /recruit/ page only. The six group companies "
                "linked there are separate legal entities and were not enumerated."
            ),
            "verification_revision": self.VERIFICATION_REVISION,
        })

        try:
            self.page_guard()
            initial_response = await self.http.get(OFFICIAL_ENTRY)
            initial_parsed = parse_official_page(initial_response.text, str(initial_response.url))
        except Exception as exc:
            self.result.errors.append(f"human_holdings_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["human_holdings_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        initial_entry = _entry_record(initial_response)
        self.result.metadata["initial_entry"] = initial_entry
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "h1": initial_parsed["h1"],
            "headings": initial_parsed["headings"],
            "group_section_found": initial_parsed["group_section_found"],
            "group_company_names": initial_parsed["group_company_names"],
            "group_company_links": initial_parsed["group_company_links"],
            "unexpected_group_names": initial_parsed["unexpected_group_names"],
            "missing_group_names": initial_parsed["missing_group_names"],
            "unexpected_group_urls": initial_parsed["unexpected_group_urls"],
            "missing_group_urls": initial_parsed["missing_group_urls"],
            "parent_contact_links": initial_parsed["parent_contact_links"],
            "job_cards": initial_parsed["job_cards"],
            "company_job_links": initial_parsed["company_job_links"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "job_filters": initial_parsed["job_filters"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "visible_text_chars": initial_parsed["visible_text_chars"],
            "normalized_text_chars": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256": initial_parsed["normalized_text_sha256"],
        }
        if initial_parsed["unexpected_group_names"] or initial_parsed["missing_group_names"]:
            self.result.warnings.append("group_company_directory_changed")
        if initial_parsed["unexpected_group_urls"] or initial_parsed["missing_group_urls"]:
            self.result.warnings.append("group_company_link_set_changed")

        if initial_parsed["has_listing_signal"]:
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_listing"] = initial_parsed["listing_signals"]
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["human_holdings_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response = await self.http.get(OFFICIAL_ENTRY)
            final_parsed = parse_official_page(final_response.text, str(final_response.url))
            self.result.metadata["final_recheck"] = _entry_record(final_response)
        except Exception as exc:
            self.result.errors.append(f"human_holdings_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["human_holdings_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        matches = False
        if final_parsed is not None:
            if final_parsed["has_listing_signal"]:
                self.result.errors.append("unreviewed_careers_listing_detected")
            matches = _evidence(initial_parsed) == _evidence(final_parsed)
            if not matches:
                self.result.errors.append("human_holdings_careers_page_changed_between_requests")

        if final_parsed is not None and matches and not final_parsed["has_listing_signal"]:
            self.result.errors.append("no_company_scoped_job_listing")

        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["scope_decision"] = {
            "scope": "company",
            "requested_company": "Human Holdings",
            "included": "Human Holdings' official /recruit/ recruitment-information page only",
            "excluded_group_companies": initial_parsed["group_company_links"],
            "exclusion_reason": (
                "The group companies are separate legal entities with their own recruitment "
                "sites. A parent page's group directory is not proof of requested-company "
                "scope; enumerating it would be a parent/group-wide board. Human Holdings "
                "itself publishes no job listing, only an inquiry form."
            ),
            "parent_contact_links": initial_parsed["parent_contact_links"],
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The official Human Holdings /recruit/ page publishes no company-scoped job "
            "listing: no job cards, job links, listing request, filters, pagination, "
            "JSON-LD JobPosting or ATS embed. Its Group Company section links six separate "
            "legal entities' own recruitment sites, and its Human Holdings card only links "
            "to the corporate inquiry form. The empty result is an explicit no-listing "
            "outcome, not a successful zero-job listing."
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["human_holdings_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
