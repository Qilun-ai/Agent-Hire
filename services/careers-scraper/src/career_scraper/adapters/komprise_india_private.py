"""Komprise's own official careers page and its own WP Job Manager listing.

The inspected journey is:

    https://www.komprise.com/company/careers/  (ledger entry, HTTP 301)
        -> https://www.komprise.com/careers/  (canonical official page)
        -> one WP Job Manager ``div.job_listings`` widget on that page
        -> POST https://www.komprise.com/jm-ajax/get_listings/
        -> cards link to https://www.komprise.com/job/<slug>/
        -> each detail page's ``.job_description`` is the full employer body.

Only the rows returned by the company's own widget are requested. No ATS board,
aggregator, guessed slug or parent-company listing is enumerated. The listing
publishes Komprise's global roles; the requested label is ``Komprise India
Private``, while the page's public employer label is ``Komprise``. All public
roles are retained (no country/seniority filter), and the source label is
preserved rather than replaced by an invented legal entity.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.komprise.com/company/careers/"
LISTING_URL = "https://www.komprise.com/careers/"
AJAX_URL = "https://www.komprise.com/jm-ajax/get_listings/"
SITEMAP_URL = "https://www.komprise.com/job_listing-sitemap.xml"
HOST = "www.komprise.com"
JOB_TYPES = ("freelance", "full-time", "internship", "part-time", "temporary")
JOB_PATH = re.compile(r"^/job/([a-z0-9][a-z0-9-]*)/$", re.I)
POST_CLASS = re.compile(r"(?:^|\s)post-(\d+)(?:\s|$)")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAILTO = re.compile(r"^mailto:([^@\s?]+@[^@\s?]+\.[^@\s?]+)(?:\?.*)?$", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
DESCRIPTION_METHOD = "komprise.wp-job-manager.single_job_listing.job_description"

RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|key duties|what you(?:'|’)?ll do|what you will do|"
    r"your role|the role|day[- ]to[- ]day",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualif|requirement|required skills|desirable skills|preferred|"
    r"what we(?:'|’)?re looking for|must have|nice to have|ideal|"
    r"minimum qualifications|experience|education",
    re.I,
)
NON_ROLE_HEADING = re.compile(
    r"^(?:location|how to apply|benefits?|company overview|job summary|"
    r"about (?:us|komprise)|compensation|salary)\b",
    re.I,
)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def comparison_text(value: str) -> str:
    """Whitespace/punctuation/case-neutral form for independent rendered-body checks.

    The live theme applies CSS ``text-transform: uppercase`` to several headings,
    and the browser's inline text flow does not add the source DOM's separator
    before a trailing full stop. The stored description keeps its original case
    and punctuation; only this comparison form is neutralised.
    """
    value = re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def comparison_body_text(html: str | None) -> str:
    return comparison_text(normalized_body_text(html))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _job_url(value: str) -> bool:
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and parts.netloc == HOST
        and JOB_PATH.fullmatch(parts.path) is not None
        and not parts.query
        and not parts.fragment
    )


def parse_official_entry(html: str, response_url: str = LISTING_URL) -> dict:
    """Validate the exact redirected careers page and its single listing widget."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != HOST or parts.path.rstrip("/") != "/careers":
        raise ValueError("Komprise official careers entry redirected away from /careers/")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at Komprise" not in title:
        raise ValueError("Komprise official careers page title changed")
    canonical = soup.select_one('link[rel="canonical"][href]')
    canonical_url = urljoin(response_url, canonical.get("href", "")) if canonical else None
    if canonical_url != LISTING_URL:
        raise ValueError("Komprise official careers page canonical URL changed")

    widgets = soup.select("div.job_listings")
    if len(widgets) != 1:
        raise ValueError("Komprise careers page no longer exposes exactly one job listing widget")
    widget = widgets[0]
    config = {
        key: widget.get(key)
        for key in (
            "data-post_id", "data-per_page", "data-orderby", "data-order",
            "data-show_filters", "data-show_pagination", "data-location",
            "data-keywords", "data-categories", "data-remote_position",
        )
    }
    try:
        per_page = int(config["data-per_page"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Komprise listing widget per-page value changed") from exc
    if per_page < 1 or per_page > 100:
        raise ValueError("Komprise listing widget per-page value is outside the observed bound")
    if config["data-show_filters"] != "true":
        raise ValueError("Komprise listing widget no longer shows its filters")
    if not str(config["data-post_id"] or "").isdigit():
        raise ValueError("Komprise listing widget page ID is missing")

    scripts = "\n".join(
        script.get_text() for script in soup.find_all("script") if not script.get("src")
    )
    match = re.search(r"var\s+job_manager_ajax_filters\s*=\s*(\{.*?\})\s*;", scripts, re.S)
    if match is None:
        raise ValueError("Komprise listing AJAX configuration is missing")
    ajax_config = json.loads(match.group(1))
    endpoint = urljoin(response_url, str(ajax_config.get("ajax_url", "")).replace("%%endpoint%%", "get_listings"))
    if endpoint != AJAX_URL:
        raise ValueError("Komprise listing AJAX endpoint changed")

    inputs = widget.select('input[name="filter_job_type[]"][type="checkbox"]')
    values = [item.get("value") for item in inputs]
    checked = [item.get("value") for item in inputs if item.has_attr("checked")]
    if tuple(values) != JOB_TYPES or set(checked) != set(JOB_TYPES):
        raise ValueError("Komprise listing job-type filters changed; membership must be re-inspected")

    listing_ul = soup.select_one("ul.job_listings")
    if listing_ul is None:
        raise ValueError("Komprise listing container is missing")
    load_more = soup.select_one("a.load_more_jobs")
    if load_more is None:
        raise ValueError("Komprise listing load-more control is missing")
    emails = sorted({
        anchor.get("href", "")
        for anchor in soup.select('a[href^="mailto:"]')
        if "careers" in anchor.get("href", "").lower()
    })
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical_url,
        "page_post_id": str(config["data-post_id"]),
        "widget": {
            "per_page": per_page,
            "orderby": config["data-orderby"],
            "order": config["data-order"],
            "show_filters": config["data-show_filters"],
            "show_pagination": config["data-show_pagination"],
            "job_types": values,
            "checked_job_types": checked,
            "keyword_search": bool(soup.select_one("input#search_keywords")),
            "location_search": bool(soup.select_one("input#search_location")),
            "remote_position_filter": bool(soup.select_one('input[name="remote_position"]')),
        },
        "ajax_endpoint": endpoint,
        "listing_placeholder_empty": not bool(listing_ul.select("li.job_listing")),
        "load_more_label": normalized(load_more.get_text(" ", strip=True)),
        "load_more_hidden": "display:none" in (load_more.get("style", "").replace(" ", "").lower()),
        "careers_emails": emails,
        "language": normalized(soup.html.get("lang")) if soup.html else None,
    }


def listing_form(page: int, per_page: int) -> dict:
    """Build the exact form serialization observed from the live widget."""
    serialized = urlencode([
        ("search_keywords", ""),
        ("search_location", ""),
        *[("filter_job_type[]", value) for value in JOB_TYPES],
        ("filter_job_type[]", ""),
    ])
    return {
        "lang": "en",
        "search_keywords": "",
        "search_location": "",
        "filter_job_type[]": [*JOB_TYPES, ""],
        "per_page": str(per_page),
        "orderby": "featured",
        "featured_first": "false",
        "order": "DESC",
        "page": str(page),
        "remote_position": "",
        "show_pagination": "false",
        "form_data": serialized,
    }


def parse_listing_response(value: dict, requested_page: int) -> dict:
    """Parse the exact company-widget response, retaining every card field."""
    if not isinstance(value, dict):
        raise ValueError("Komprise listing AJAX response is not an object")
    found = value.get("found_jobs")
    max_pages = value.get("max_num_pages")
    html = value.get("html")
    if found not in (True, False):
        raise ValueError("Komprise listing AJAX response is missing found_jobs")
    if not isinstance(max_pages, int) or max_pages < 0:
        raise ValueError("Komprise listing AJAX response has an invalid max_num_pages")
    if not isinstance(html, str):
        raise ValueError("Komprise listing AJAX response has no html field")
    if found and (max_pages < 1 or not html.strip()):
        raise ValueError("Komprise listing AJAX response is empty for a non-empty result")

    soup = BeautifulSoup(html, "lxml")
    rows, issues, seen = [], [], set()
    for item in soup.select("li.job_listing"):
        classes = " ".join(item.get("class") or [])
        post_match = POST_CLASS.search(classes)
        anchor = item.select_one("a[href]")
        heading = item.select_one("h3")
        if post_match is None:
            issues.append("listing_card_missing_post_id")
            continue
        identity = post_match.group(1)
        if anchor is None:
            issues.append(f"listing_card_missing_link:{identity}")
            continue
        url = urljoin(LISTING_URL, anchor.get("href", ""))
        if not _job_url(url):
            issues.append(f"listing_card_not_company_job_link:{identity}:{url}")
            continue
        title = normalized(heading.get_text(" ", strip=True)) if heading else ""
        if not title:
            issues.append(f"listing_card_missing_title:{identity}")
            continue
        if identity in seen:
            issues.append(f"duplicate_company_job_id:{identity}")
            continue
        location_node = item.select_one(".location")
        type_node = item.select_one(".job-type")
        time_node = item.select_one("time[datetime]")
        company_node = item.select_one(".company")
        date_posted = None
        date_text = None
        if time_node is not None:
            date_posted = normalized(time_node.get("datetime"))
            date_text = normalized(time_node.get_text(" ", strip=True))
            if not DATE_ONLY.fullmatch(date_posted):
                issues.append(f"listing_card_invalid_date:{identity}:{date_posted}")
        else:
            issues.append(f"listing_card_missing_date:{identity}")
        row = {
            "id": identity,
            "url": url,
            "title": title,
            "location": normalized(location_node.get_text(" ", strip=True)) if location_node else None,
            "job_type": normalized(type_node.get_text(" ", strip=True)) if type_node else None,
            "date_posted": date_posted,
            "date_posted_text": date_text,
            "company": normalized(company_node.get_text(" ", strip=True)) if company_node else None,
            "card_html": str(item),
        }
        if not row["location"]:
            issues.append(f"listing_card_missing_location:{identity}")
        if not row["company"]:
            issues.append(f"listing_card_missing_company:{identity}")
        seen.add(identity)
        rows.append(row)

    showing = normalized(value.get("showing") or "")
    advertised = None
    count_match = re.search(r"Found\s+(\d+)\s+matching", showing, re.I)
    if count_match:
        advertised = int(count_match.group(1))
    if not found and rows:
        issues.append("listing_found_jobs_false_but_rows_present")
    if found and not rows:
        issues.append("listing_found_jobs_true_but_no_rows")
    if advertised is not None and advertised != len(rows):
        issues.append(f"listing_advertised_count_mismatch:{advertised}:{len(rows)}")
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "found_jobs": found,
        "max_num_pages": max_pages,
        "requested_page": requested_page,
        "showing": showing,
        "advertised_count": advertised,
        "pagination": value.get("pagination"),
        "issues": issues,
    }


def parse_sitemap(xml_text: str) -> dict:
    """Read the page's own job sitemap as an independent membership cross-check."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Komprise job sitemap is not valid XML: {exc}") from exc
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    rows, other = [], []
    for node in root.findall("sm:url", namespace):
        loc = (node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        lastmod = (node.findtext("sm:lastmod", default="", namespaces=namespace) or "").strip() or None
        if not loc:
            continue
        if _job_url(loc):
            rows.append({"url": loc, "lastmod": lastmod})
        else:
            other.append({"url": loc, "lastmod": lastmod})
    return {
        "job_urls": [row["url"] for row in rows],
        "job_rows": rows,
        "other_urls": [row["url"] for row in other],
        "other_rows": other,
    }


def description_sections(html: str) -> list[dict]:
    """Keep every top-level employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-komprise-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-komprise-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        level = None
        heading_html = ""
        subheading = False
        if block.name in HEADING_TAGS:
            heading = normalized(block.get_text(" ", strip=True)).rstrip(":").strip()
            level = int(block.name[1])
            heading_html = str(block)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized(block.get_text(" ", strip=True))
            strong_text = normalized(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text.rstrip(":").strip()
                level = 7
                heading_html = str(block)
                subheading = True
        if heading:
            if current is not None:
                sections.append(current)
            current = {
                "heading": heading,
                "level": level,
                "subheading": subheading,
                "heading_html": heading_html,
                "html": "",
                "text": "",
            }
            continue
        if current is None:
            current = {"heading": None, "level": None, "subheading": False, "heading_html": "", "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = text_html(section["html"]) if section["html"] else ""
    return sections


def matching_sections(sections: list[dict], pattern: re.Pattern) -> list[dict]:
    """Mirror a heading section and its deeper sub-headings into a structured field."""
    matched = []
    for index, section in enumerate(sections):
        if not section["heading"] or not pattern.search(section["heading"]):
            continue
        level = section["level"] if section["level"] is not None else 0
        if section["text"]:
            matched.append({"heading": section["heading"], "text": section["text"]})
        for child in sections[index + 1:]:
            if not child["heading"]:
                continue
            if child["subheading"]:
                if NON_ROLE_HEADING.search(child["heading"]):
                    continue
                if child["text"]:
                    matched.append({"heading": child["heading"], "text": child["text"]})
                continue
            child_level = child["level"] if child["level"] is not None else 0
            if child_level <= level:
                break
            if child["text"]:
                matched.append({"heading": child["heading"], "text": child["text"]})
    return matched


def _is_external_application_url(value: str) -> bool:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
        return False
    # The observed WP Job Manager malformed mailto is http://<email>@komprise.com;
    # username is set above, so it is never mistaken for an application form.
    return parts.netloc != HOST or bool(parts.path.strip("/")) or bool(parts.query)


def _application_route(soup: BeautifulSoup, description: Tag, job_url: str) -> dict:
    application = soup.select_one(".job_application")
    application_text = normalized(application.get_text(" ", strip=True)) if application else ""
    application_links = [
        {"text": normalized(anchor.get_text(" ", strip=True)), "url": anchor.get("href", "")}
        for anchor in (application.select("a[href]") if application else [])
    ]
    description_links = [
        {"text": normalized(anchor.get_text(" ", strip=True)), "url": urljoin(job_url, anchor.get("href", ""))}
        for anchor in description.select("a[href]")
    ]

    def mailto_address(href: str) -> str | None:
        match = MAILTO.fullmatch(href or "")
        return match.group(1) if match else None

    application_mailtos = [link["url"] for link in application_links if mailto_address(link["url"])]
    description_mailtos = [link["url"] for link in description_links if mailto_address(link["url"])]
    external = [link["url"] for link in application_links
                if _is_external_application_url(link["url"])
                and urljoin(job_url, link["url"]) != job_url]
    malformed = [
        link for link in application_links
        if not mailto_address(link["url"])
        and not _is_external_application_url(link["url"])
        and link["url"] != job_url
    ]
    selected = application_mailtos[0] if application_mailtos else (
        description_mailtos[0] if description_mailtos else (external[0] if external else None)
    )
    if selected and selected.startswith("mailto:"):
        kind = "email"
    elif selected:
        kind = "external_form"
    else:
        kind = "job_page_only"
    return {
        "kind": kind,
        "apply_url": selected or job_url,
        "email": mailto_address(selected) if selected and selected.startswith("mailto:") else None,
        "application_block_text": application_text,
        "application_block_links": application_links,
        "description_mailto_links": description_mailtos,
        "application_mailto_links": application_mailtos,
        "external_application_links": external,
        "malformed_application_links": malformed,
        "resolved_from_description_mailto": bool(selected and not application_mailtos and description_mailtos),
    }


def parse_job_detail(html: str, row: dict, response_url: str | None = None) -> dict:
    """Parse one exact company-linked detail page and reconcile it with its card."""
    expected_url = row["url"]
    response_url = response_url or expected_url
    if response_url != expected_url:
        raise ValueError(f"Komprise detail page redirected away from its exact card URL: {response_url}")
    if not _job_url(expected_url):
        raise ValueError(f"Komprise detail row is not an exact company job URL: {expected_url}")
    soup = BeautifulSoup(html, "lxml")
    containers = soup.select(".single_job_listing")
    if len(containers) != 1:
        raise ValueError("Komprise detail page no longer has exactly one .single_job_listing")
    container = containers[0]
    h1s = soup.select("h1")
    if len(h1s) != 1:
        raise ValueError("Komprise detail page no longer has exactly one h1")
    title = normalized(h1s[0].get_text(" ", strip=True))
    if title != row["title"]:
        raise ValueError(f"Komprise detail title changed: {title!r} != {row['title']!r}")
    identity = row["id"]
    body_classes = soup.body.get("class") if soup.body else []
    if f"postid-{identity}" not in (body_classes or []):
        raise ValueError(f"Komprise detail page post ID does not match listing card {identity}")

    description = container.select_one(".job_description")
    if description is None:
        raise ValueError("Komprise detail page has no .job_description body")
    description_html = description.decode_contents()
    description_text = text_html(description_html)
    normalized_text = normalized_body_text(description_html)
    if not description_text.strip() or len(normalized_text) < 100:
        raise ValueError("Komprise .job_description body is empty or too short for a full posting")
    if re.search(r"(?:\.\.\.|…)\s*(?:read more|show more|learn more)$", normalized_text, re.I):
        raise ValueError("Komprise .job_description body ends in a truncated preview")

    meta = container.select_one("ul.meta")
    if meta is None:
        raise ValueError("Komprise detail page has no job metadata list")
    type_li = meta.select_one("li.job-type")
    location_li = meta.select_one("li.location")
    date_li = meta.select_one("li.date-posted")
    detail_type = normalized(type_li.get_text(" ", strip=True)) if type_li else None
    detail_location = normalized(location_li.get_text(" ", strip=True)) if location_li else None
    time_node = date_li.select_one("time[datetime]") if date_li else None
    detail_date = normalized(time_node.get("datetime")) if time_node else None
    detail_date_text = normalized(time_node.get_text(" ", strip=True)) if time_node else None
    if row.get("job_type") != detail_type:
        raise ValueError(f"Komprise detail job type changed: {detail_type!r} != {row.get('job_type')!r}")
    if row.get("location") != detail_location:
        raise ValueError(f"Komprise detail location changed: {detail_location!r} != {row.get('location')!r}")
    if row.get("date_posted") != detail_date:
        raise ValueError(f"Komprise detail posting date changed: {detail_date!r} != {row.get('date_posted')!r}")

    company_node = container.select_one(".company .name") or container.select_one(".company")
    employer = normalized(company_node.get_text(" ", strip=True)) if company_node else None
    if not employer or ("Komprise" not in employer):
        raise ValueError("Komprise detail page no longer identifies Komprise as the employer")
    if row.get("company") and row["company"] != employer:
        raise ValueError(f"Komprise listing/detail employer mismatch: {row['company']!r} != {employer!r}")

    sections = description_sections(description_html)
    responsibilities = matching_sections(sections, RESPONSIBILITY_HEADING)
    qualifications = matching_sections(sections, QUALIFICATION_HEADING)
    links = [
        {"text": normalized(anchor.get_text(" ", strip=True)), "url": urljoin(response_url, anchor.get("href", ""))}
        for anchor in description.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    application = _application_route(soup, description, expected_url)
    updated_meta = soup.select_one('meta[property="article:modified_time"]')
    date_updated = normalized(updated_meta.get("content")) if updated_meta else None
    if date_updated:
        try:
            datetime.fromisoformat(date_updated.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Komprise article:modified_time is not an ISO timestamp: {date_updated!r}") from exc

    return {
        "title": title,
        "job_id": identity,
        "url": expected_url,
        "apply_url": application["apply_url"],
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_text": normalized_text,
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_normalized_sha256": sha256(normalized_text.encode()).hexdigest(),
        "description_normalized_chars": len(normalized_text),
        "description_fnv1a64": fnv1a64(normalized_text),
        "description_comparison_text": comparison_text(normalized_text),
        "description_comparison_chars": len(comparison_text(normalized_text)),
        "description_comparison_sha256": sha256(comparison_text(normalized_text).encode()).hexdigest(),
        "description_comparison_fnv1a64": fnv1a64(comparison_text(normalized_text)),
        "locations": [detail_location] if detail_location else [],
        "employment_type": detail_type,
        "workplace_type": "Remote" if detail_location and "remote" in detail_location.lower() else None,
        "date_posted": detail_date,
        "date_posted_text": detail_date_text,
        "date_updated": date_updated,
        "valid_through": None,
        "salary": None,
        "department": None,
        "employer": employer,
        "language": normalized(soup.html.get("lang")) if soup.html else None,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "description_sections": sections,
        "description_links": links,
        "role_attachments": attachments,
        "application_route": application,
        "page": {
            "post_id": identity,
            "title": normalized(soup.title.get_text(" ", strip=True) if soup.title else ""),
            "h1": title,
            "body_classes": body_classes,
            "date_updated": date_updated,
            "meta": {
                "job_type": detail_type,
                "location": detail_location,
                "date_posted": detail_date,
                "date_posted_text": detail_date_text,
                "date_updated": date_updated,
            },
        },
    }


class KompriseIndiaPrivate(Adapter):
    """Collect exactly the public rows of Komprise's own WP Job Manager widget."""

    VERIFICATION_REVISION = "komprise-india-private.official-wp-job-manager.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._timings: dict[str, float] = {
            "entry_request_seconds": 0.0,
            "listing_request_seconds": 0.0,
            "sitemap_request_seconds": 0.0,
            "detail_request_seconds_sum": 0.0,
            "detail_parse_seconds_sum": 0.0,
        }

    async def _fetch(self, method: str, url: str, purpose: str, **kwargs):
        started = time.perf_counter()
        try:
            response = await self.http.request(method, url, **kwargs)
        except Exception as exc:
            self.result.metadata.setdefault("journey", []).append({
                "purpose": purpose, "url": url, "outcome": "request_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "request_seconds": max(0.0, time.perf_counter() - started),
            })
            raise
        seconds = max(0.0, time.perf_counter() - started)
        record = {
            "purpose": purpose,
            "url": url,
            "outcome": "received",
            "final_url": str(response.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "redirects": [str(item.url) for item in response.history],
            "request_seconds": seconds,
        }
        self.result.metadata.setdefault("journey", []).append(record)
        return response, record

    async def _listing_page(self, page: int, per_page: int):
        if not self.page_guard():
            raise RuntimeError("Komprise listing page budget exhausted")
        response, record = await self._fetch(
            "POST", AJAX_URL, f"company_listing_page_{page}",
            data=listing_form(page, per_page),
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": LISTING_URL,
                "Origin": "https://www.komprise.com",
            },
        )
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.netloc != HOST or parts.path != "/jm-ajax/get_listings/":
            raise ValueError("Komprise listing AJAX redirected away from its widget route")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Komprise listing AJAX did not return JSON") from exc
        parsed = parse_listing_response(payload, page)
        self._timings["listing_request_seconds"] += record["request_seconds"]
        return parsed, record

    async def _detail(self, job):
        started = time.perf_counter()
        response, record = await self._fetch("GET", job.url, "exact_company_job_detail")
        self._timings["detail_request_seconds_sum"] += record["request_seconds"]
        parsed = parse_job_detail(response.text, job.raw["listing"], str(response.url))
        parse_seconds = max(0.0, time.perf_counter() - started) - record["request_seconds"]
        self._timings["detail_parse_seconds_sum"] += max(0.0, parse_seconds)
        job.title = parsed["title"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = DESCRIPTION_METHOD
        job.description_source_url = job.url
        job.locations = parsed["locations"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = {
            "datetime": parsed["date_posted"],
            "text": parsed["date_posted_text"],
            "source": "listing card and detail page time[datetime]",
        }
        job.date_updated = parsed["date_updated"]
        job.valid_through = parsed["valid_through"]
        job.salary = parsed["salary"]
        job.department = parsed["department"]
        job.employer = parsed["employer"]
        job.language = parsed["language"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.raw["detail"] = {
            "source_field": "div.single_job_listing .job_description innerHTML",
            "description_sha256": parsed["description_sha256"],
            "description_normalized_sha256": parsed["description_normalized_sha256"],
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_comparison_chars": parsed["description_comparison_chars"],
            "description_comparison_sha256": parsed["description_comparison_sha256"],
            "description_comparison_fnv1a64": parsed["description_comparison_fnv1a64"],
            "description_sections": parsed["description_sections"],
            "description_links": parsed["description_links"],
            "role_attachments": parsed["role_attachments"],
            "application_route": parsed["application_route"],
            "page": parsed["page"],
            "response": record,
        }
        return job

    def _make_job(self, row: dict, entry: dict):
        location = row.get("location")
        job = self.job(
            {"listing": row, "detail": {}},
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            description="",
            method=None,
            description_url=None,
            locations=[location] if location else [],
            employment_type=row.get("job_type"),
            workplace_type="Remote" if location and "remote" in location.lower() else None,
            date_posted=row.get("date_posted"),
            date_posted_raw={
                "datetime": row.get("date_posted"),
                "text": row.get("date_posted_text"),
                "source": "listing card time[datetime]",
            },
            employer=row.get("company"),
            language=entry.get("language"),
            requisition_id=None,
            apply_url=row["url"],
        )
        job.raw["company_listing_card"] = row
        return job

    async def run(self):
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "The exact WP Job Manager widget on Komprise's own official careers page.",
            "listing_membership_request": {
                "method": "POST",
                "url": AJAX_URL,
                "query_shape": listing_form(1, 10),
                "filters_applied": False,
                "scope": "Komprise's own careers widget; no ATS, aggregator or parent board.",
                "browser_evidence": "companies/komprise-india-private/browser-reference-worker.json",
            },
            "detail_method": DESCRIPTION_METHOD,
        })
        entry = None
        try:
            entry_response, entry_record = await self._fetch("GET", OFFICIAL_ENTRY, "official_entry")
            self._timings["entry_request_seconds"] += entry_record["request_seconds"]
            if str(entry_response.url) != LISTING_URL:
                raise ValueError(f"Komprise entry redirected to {entry_response.url}, not {LISTING_URL}")
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            self.result.metadata["official_entry"] = entry
            self.result.metadata["official_entry_response"] = entry_record
        except Exception as exc:
            self.result.errors.append(f"official_entry: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.finish()
            return self.result

        sitemap = None
        sitemap_record = None
        try:
            sitemap_response, sitemap_record = await self._fetch("GET", SITEMAP_URL, "job_sitemap")
            self._timings["sitemap_request_seconds"] += sitemap_record["request_seconds"]
            sitemap = parse_sitemap(sitemap_response.text)
            self.result.metadata["job_sitemap"] = sitemap
            self.result.metadata["job_sitemap_response"] = sitemap_record
        except Exception as exc:
            self.result.errors.append(f"job_sitemap: {type(exc).__name__}: {exc}")

        per_page = entry["widget"]["per_page"]
        pages, rows, issues = [], [], []
        max_pages = None
        page = 1
        while True:
            try:
                parsed, record = await self._listing_page(page, per_page)
            except Exception as exc:
                self.result.errors.append(f"company_listing_page_{page}: {type(exc).__name__}: {exc}")
                break
            if max_pages is None:
                max_pages = parsed["max_num_pages"]
            elif parsed["max_num_pages"] != max_pages:
                issues.append("listing_max_num_pages_changed_between_pages")
            issues.extend(parsed["issues"])
            known = {row["id"] for row in rows}
            for row in parsed["rows"]:
                if row["id"] in known:
                    issues.append(f"duplicate_company_job_id_across_pages:{row['id']}")
                    continue
                rows.append(row)
            pages.append({
                "page": page,
                "status": record["status"],
                "final_url": record["final_url"],
                "request_seconds": record["request_seconds"],
                "response_sha256": record["sha256"],
                "response_bytes": record["response_bytes"],
                "job_ids": parsed["job_ids"],
                "max_num_pages": parsed["max_num_pages"],
                "showing": parsed["showing"],
                "advertised_count": parsed["advertised_count"],
            })
            if max_pages is None or max_pages < 1 or page >= max_pages:
                break
            page += 1
        for issue in issues:
            self.result.errors.append(f"listing: {issue}")
        self.result.metadata["listing_pages"] = pages

        listing_urls = [row["url"] for row in rows]
        listing_ids = [row["id"] for row in rows]
        sitemap_urls = sitemap["job_urls"] if sitemap else []
        sitemap_set, listing_set = set(sitemap_urls), set(listing_urls)
        sitemap_matches = bool(sitemap) and sitemap_set == listing_set
        if sitemap and not sitemap_matches:
            self.result.errors.append(
                "listing_sitemap_membership_mismatch:"
                f"missing_from_sitemap={sorted(listing_set - sitemap_set)}:"
                f"missing_from_listing={sorted(sitemap_set - listing_set)}"
            )
        if len(listing_ids) != len(listing_set):
            self.result.errors.append("listing_duplicate_ids_after_reconciliation")
        if not rows:
            self.result.warnings.append("empty_public_listing_observed")
            self.result.expected_total = 0 if sitemap_matches else None
        else:
            self.result.expected_total = len(rows)

        for row in rows:
            self.add(self._make_job(row, entry))
        if rows:
            await self.hydrate(self.result.jobs, self._detail)

        failed_jobs = []
        for job in self.result.jobs:
            if not job.description_verified:
                failed_jobs.append({
                    "id": job.job_id,
                    "url": job.url,
                    "reason": "; ".join(job.warnings) or "description_not_verified",
                })
                self.result.errors.append(f"description_not_verified:{job.job_id}")
        jobs_by_id = {str(job.job_id): job for job in self.result.jobs}
        identity_ok = set(jobs_by_id) == set(listing_ids)
        if not identity_ok:
            self.result.errors.append("collected_job_ids_do_not_match_listing_ids")
        for row in rows:
            job = jobs_by_id.get(row["id"])
            if job is None:
                self.result.errors.append(f"missing_collected_job:{row['id']}")
                continue
            if job.url != row["url"] or job.title != row["title"]:
                self.result.errors.append(f"collected_job_identity_mismatch:{row['id']}")
            if job.raw.get("detail", {}).get("page", {}).get("post_id") != row["id"]:
                self.result.errors.append(f"detail_post_id_mismatch:{row['id']}")

        reconciliation = {
            "listing_rows": len(rows),
            "listing_ids": listing_ids,
            "listing_urls": listing_urls,
            "listing_pages": len(pages),
            "max_num_pages": max_pages,
            "per_page": per_page,
            "sitemap_job_urls": sitemap_urls,
            "sitemap_matches_listing": sitemap_matches,
            "collected_jobs": len(self.result.jobs),
            "descriptions_verified": sum(job.description_verified for job in self.result.jobs),
            "identity_matches_listing": identity_ok,
            "issues": list(dict.fromkeys(issues)),
            "complete": bool(
                rows
                and sitemap_matches
                and identity_ok
                and not self.result.errors
                and all(job.description_verified for job in self.result.jobs)
            ),
        }
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["failed_jobs"] = failed_jobs
        self.result.metadata["timings"] = {
            **self._timings,
            "source_collection_seconds": (
                self._timings["entry_request_seconds"]
                + self._timings["listing_request_seconds"]
                + self._timings["sitemap_request_seconds"]
            ),
        }
        self.result.listing_complete = reconciliation["complete"]
        self.result.finish()
        return self.result
