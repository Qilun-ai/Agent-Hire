"""MaxLinear's official careers page and the two company-published iCIMS portals it links.

The published journey (observed 2026-11-07):
  https://www.maxlinear.com/company/careers
    -> "Browse all US Jobs"          https://careersus-maxlinear.icims.com/jobs/search?ss=1
    -> "Browse all International Jobs" https://careersintl-maxlinear.icims.com/jobs/search?ss=1

Both entry links open MaxLinear-branded iCIMS career sites.  Each portal search
page is a shell around one iframe with the real listing
(``/jobs/search?ss=1&in_iframe=1``); pagination is ``/jobs/search?pr=N&in_iframe=1``
with 20 cards per page, and the page header ("Search Results Page X of Y") is the
only published result count.  Job details are ``/jobs/{id}/{slug}/job`` with all
sections under ``div.iCIMS_JobContent`` and a schema.org ``JobPosting`` JSON-LD
twin of the visible body.

Posting dates: the international portal publishes a real ``datePosted`` (clean
midnight-UTC values consistent with job ids).  The US portal publishes no date;
iCIMS fills ``datePosted`` with a request-time template (fetch time minus two
years), which is never stored as a posting date.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

VERIFICATION_REVISION = "maxlinear-icims-2026-11-07"

OFFICIAL_ENTRY = "https://www.maxlinear.com/company/careers"
PORTALS = {
    "us": "https://careersus-maxlinear.icims.com",
    "intl": "https://careersintl-maxlinear.icims.com",
}
PORTAL_LABELS = {
    "us": "Browse all US Jobs",
    "intl": "Browse all International Jobs",
}
LISTING_FIRST = "/jobs/search?ss=1&in_iframe=1"
LISTING_PAGE = "/jobs/search?pr={page}&in_iframe=1"
JOB_PATH = re.compile(r"^/jobs/(\d+)/([^/]+)/job$")
PAGE_HEADER = re.compile(r"Search Results\s+Page\s+(\d+)\s+of\s+(\d+)", re.I)
EXTERNAL_ID = re.compile(r"^(\d{4})-(\d+)$")
SALARY_RANGE = re.compile(
    r"\$([\d,]{4,})\s*(?:to|–|-)\s*\$([\d,]{4,})(?:\s*(Annually|annually|per year|hourly|Hourly))?"
)
SECTION_LABELS = ("Responsibilities", "Qualifications", "Compensation and Benefits", "Company Overview")
MISSING_VALUES = {"", "UNAVAILABLE", "N/A", "NA", "NONE"}


def normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFC", (value or "").replace("\xa0", " "))
    return re.sub(r"\s+", " ", value).strip()


def space_key(value: str | None) -> str:
    return re.sub(r"\s+", "", normalized(value))


def _portal_for_host(host: str) -> str | None:
    for portal, origin in PORTALS.items():
        if urlsplit(origin).netloc == host:
            return portal
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Verify MaxLinear's own careers page still publishes both iCIMS portals."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc not in {"www.maxlinear.com", "maxlinear.com"}
            or parts.path.rstrip("/") != "/company/careers"):
        raise ValueError(f"MaxLinear official entry redirected away from its inspected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "MaxLinear" not in title or not re.search(r"Careers", title, re.I):
        raise ValueError("MaxLinear official careers page identity changed")

    portal_links: dict[str, dict] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        target = urlsplit(href)
        portal = _portal_for_host(target.netloc)
        if portal is None or target.path != "/jobs/search":
            continue
        if parse_qs(target.query).get("ss") != ["1"]:
            continue
        label = normalized(anchor.get_text(" ", strip=True))
        if label in PORTAL_LABELS.values():
            portal_links.setdefault(portal, {"url": href, "label": label})
    missing = [portal for portal in PORTALS if portal not in portal_links]
    if missing:
        raise ValueError(f"MaxLinear careers page no longer links its inspected portal(s): {missing}")
    if {portal_links[p]["label"] for p in portal_links} != set(PORTAL_LABELS.values()):
        raise ValueError("MaxLinear portal link labels changed")

    regions = []
    for anchor in soup.select(".jobs-search .dropdown-menu a, .jobs-search .dropdown-item"):
        label = normalized(anchor.get_text(" ", strip=True))
        if label and label not in regions:
            regions.append(label)
    if not {"United States", "International"}.issubset(set(regions)):
        raise ValueError("MaxLinear job-search region selector changed")

    featured = []
    for card in soup.select(".jobs-slider .card"):
        anchor = card.select_one(".card-footer a[href]")
        if anchor is None:
            continue
        target = urlsplit(anchor["href"])
        portal = _portal_for_host(target.netloc)
        match = JOB_PATH.fullmatch(target.path)
        if portal is None or match is None:
            continue
        title_node = card.select_one(".title")
        label_node = card.select_one(".label")
        location_node = card.select_one(".location span")
        featured.append({
            "portal": portal,
            "job_id": match.group(1),
            "slug": unquote(match.group(2)),
            "title": normalized(title_node.get_text(" ", strip=True)) if title_node else None,
            "department": normalized(label_node.get_text(" ", strip=True)) if label_node else None,
            "location": normalized(location_node.get_text(" ", strip=True)) if location_node else None,
            "url": f"{PORTALS[portal]}{target.path}",
        })

    return {
        "url": parts.geturl(),
        "title": title,
        "portal_links": portal_links,
        "region_options": regions,
        "featured": featured,
    }


def parse_listing(html: str, response_url: str) -> dict:
    """Parse one iCIMS search-results page (``in_iframe=1`` variant)."""
    parts = urlsplit(response_url)
    portal = _portal_for_host(parts.netloc)
    if portal is None:
        raise ValueError(f"MaxLinear listing response came from an unexpected host: {response_url}")
    if parts.path != "/jobs/search":
        raise ValueError(f"MaxLinear listing response is not the inspected search route: {response_url}")
    query = parse_qs(parts.query)
    if query.get("in_iframe") != ["1"]:
        raise ValueError("MaxLinear listing response is not the inspected iframe listing")
    if any(key in query for key in ("searchKeyword", "searchRelation", "searchCategory", "searchLocation")):
        raise ValueError(f"MaxLinear listing unexpectedly filtered: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "MaxLinear" not in title:
        raise ValueError("MaxLinear listing page identity changed")
    header = soup.select_one("h2.iCIMS_SubHeader_Jobs")
    match = PAGE_HEADER.search(normalized(header.get_text(" ", strip=True)) if header else "")
    if match is None:
        raise ValueError(f"MaxLinear listing page header missing: {response_url}")
    page_no, total_pages = int(match.group(1)), int(match.group(2))
    if page_no < 1 or total_pages < 1 or page_no > total_pages:
        raise ValueError(f"MaxLinear listing page header out of range: {page_no}/{total_pages}")

    cards = []
    for card in soup.select("ul.iCIMS_JobsTable > li.iCIMS_JobCardItem"):
        anchor = card.select_one("div.title a[href]")
        if anchor is None:
            raise ValueError("MaxLinear listing card lost its title link")
        target = urlsplit(anchor["href"])
        if target.netloc != parts.netloc:
            raise ValueError(f"MaxLinear listing card links another host: {anchor['href']}")
        job_match = JOB_PATH.fullmatch(target.path)
        if job_match is None:
            raise ValueError(f"MaxLinear listing card is not an inspected job route: {anchor['href']}")
        heading = anchor.find("h3")
        if heading is not None:
            card_title = normalized(heading.get_text(" ", strip=True))
        else:
            # The <a> carries an sr-only field label before the visible title.
            for label in anchor.select("span.sr-only"):
                label.decompose()
            card_title = normalized(anchor.get_text(" ", strip=True))
        if not card_title:
            raise ValueError("MaxLinear listing card has no title")

        fields = {}
        for tag in card.select("dl.iCIMS_JobHeaderGroup div.iCIMS_JobHeaderTag"):
            key = normalized(tag.select_one("dt").get_text(" ", strip=True))
            fields[key] = normalized(tag.select_one("dd").get_text(" ", strip=True))
        external_id = fields.get("ID")
        if not external_id:
            raise ValueError(f"MaxLinear listing card {card_title!r} has no ID")
        id_match = EXTERNAL_ID.fullmatch(external_id)
        if id_match is None or id_match.group(2) != job_match.group(1):
            raise ValueError(f"MaxLinear listing card ID {external_id!r} does not match its route")
        location = fields.get("Location : Location") or fields.get("Job Locations")
        if not location:
            node = card.select_one("div.header.left span:not(.sr-only)")
            location = normalized(node.get_text(" ", strip=True)) if node else None
        preview = card.select_one("div.description")
        cards.append({
            "portal": portal,
            "job_id": job_match.group(1),
            "slug": unquote(job_match.group(2)),
            "title": card_title,
            "external_id": external_id,
            "header_fields": fields,
        "category": fields.get("Category"),
            "location": location,
            "url": f"{PORTALS[portal]}{target.path}",
            "listing_url": response_url,
            "preview_text": normalized(preview.get_text(" ", strip=True))[:400] if preview else None,
        })
    if not cards:
        raise ValueError(f"MaxLinear listing page {page_no}/{total_pages} published no job cards")
    if page_no < total_pages and len(cards) != 20:
        raise ValueError(f"MaxLinear listing page {page_no}/{total_pages} is not the observed 20-card page")
    return {"portal": portal, "page_no": page_no, "total_pages": total_pages, "cards": cards}


def _section_rows(content) -> list[dict]:
    rows = []
    nodes = list(content.find_all(recursive=False))
    for index, node in enumerate(nodes):
        if node.name != "h2" or "iCIMS_InfoField_Job" not in (node.get("class") or []):
            continue
        body = nodes[index + 1] if index + 1 < len(nodes) else None
        if body is None or body.name != "div" or "iCIMS_InfoMsg_Job" not in (body.get("class") or []):
            raise ValueError("MaxLinear detail section structure changed")
        label = normalized(node.get_text(" ", strip=True))
        text_node = body.select_one(".iCIMS_Expandable_Text") or body
        rows.append({"label": label, "text": text_html(text_node.decode_contents()),
                     "html": text_node.decode_contents()})
    return rows


def _address(address: dict) -> dict:
    keys = (("street", "streetAddress"), ("city", "addressLocality"), ("region", "addressRegion"),
            ("postal_code", "postalCode"), ("country", "addressCountry"))
    row = {}
    for output, source in keys:
        value = normalized(address.get(source))
        if value and value.upper() not in MISSING_VALUES:
            row[output] = value
    return row


def _salary(text: str) -> dict | None:
    value = normalized(text)
    if not value:
        return None
    compact = re.sub(r"\$\s*", "$", value)
    compact = re.sub(r"(?<=\d)\s*,\s*(?=\d)", ",", compact)
    compact = re.sub(r"(?<=\d)\s+(?=\d{4}\b)", "", compact)
    ranges = []
    for match in SALARY_RANGE.finditer(compact):
        context = compact[max(0, match.start() - 90):match.start()].strip(" .:")
        periods = {"annually": "annually", "per year": "annually", "hourly": "hourly"}
        period = periods.get(normalized(match.group(3)).lower()) if match.group(3) else None
        ranges.append({
            "min": int(match.group(1).replace(",", "")),
            "max": int(match.group(2).replace(",", "")),
            "currency": "USD",
            "period": period,
            "context": context[-70:],
        })
    return {"source_section": "Compensation and Benefits", "text": value, "ranges": ranges}


def source_posted_date(raw: str | None, observed_at: datetime) -> tuple[str | None, str]:
    """Accept only genuine iCIMS posted dates, never the request-time template.

    The US portal fills ``datePosted`` with (fetch time - 2 years) to the
    millisecond; genuine international dates are clean midnight-UTC values
    (04:00/05:00Z, i.e. midnight US Eastern) and therefore end ``.000Z``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "absent"
    if not raw.endswith(".000Z"):
        return None, "request_derived_template"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, "unparseable"
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None, "non_utc"
    if parsed.strftime("%H:%M:%S") not in {"04:00:00", "05:00:00"}:
        return None, "non_source_format"
    if parsed > observed_at + timedelta(days=2):
        return None, "future"
    return parsed.date().isoformat(), "jsonld"


def parse_detail(html: str, response_url: str, *, expected_portal: str, expected_job_id: str,
                 expected_slug: str | None = None, expected_external_id: str | None = None,
                 expected_title: str | None = None, observed_at: datetime | None = None) -> dict:
    """Parse one exact iCIMS job detail and verify its identity and description twin."""
    parts = urlsplit(response_url)
    portal = _portal_for_host(parts.netloc)
    if portal is None or portal != expected_portal:
        raise ValueError(f"MaxLinear detail came from an unexpected portal: {response_url}")
    job_match = JOB_PATH.fullmatch(parts.path)
    if job_match is None:
        raise ValueError(f"MaxLinear detail is not an inspected job route: {response_url}")
    if job_match.group(1) != expected_job_id:
        raise ValueError(f"MaxLinear detail id {job_match.group(1)!r} != listed {expected_job_id!r}")
    detail_slug = unquote(job_match.group(2))
    if expected_slug is not None and detail_slug != expected_slug:
        raise ValueError(f"MaxLinear detail slug {detail_slug!r} != listed {expected_slug!r}")
    if parse_qs(parts.query).get("in_iframe") != ["1"]:
        raise ValueError("MaxLinear detail response is not the inspected iframe body")
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("div.iCIMS_JobContent")
    if content is None:
        raise ValueError(f"MaxLinear detail {expected_job_id} lost its job content container")
    heading = content.select_one("h1.iCIMS_Header")
    if heading is None:
        raise ValueError(f"MaxLinear detail {expected_job_id} lost its title")
    title = normalized(heading.get_text(" ", strip=True))
    if not title:
        raise ValueError(f"MaxLinear detail {expected_job_id} has an empty title")
    if expected_title is not None and title != normalized(expected_title):
        raise ValueError(f"MaxLinear detail title {title!r} != listing {expected_title!r}")

    fields = {}
    for tag in content.select("dl.iCIMS_JobHeaderGroup div.iCIMS_JobHeaderTag"):
        key = normalized(tag.select_one("dt").get_text(" ", strip=True))
        fields[key] = normalized(tag.select_one("dd").get_text(" ", strip=True))
    external_id = fields.get("ID")
    if not external_id:
        raise ValueError(f"MaxLinear detail {expected_job_id} has no source ID")
    id_match = EXTERNAL_ID.fullmatch(external_id)
    if id_match is None or id_match.group(2) != expected_job_id:
        raise ValueError(f"MaxLinear detail ID {external_id!r} does not match its route")
    if expected_external_id is not None and external_id != expected_external_id:
        raise ValueError(f"MaxLinear detail ID {external_id!r} != listing {expected_external_id!r}")

    location_sources = []
    header_node = content.select_one("div.header.left span:not(.sr-only)")
    if header_node is not None:
        location_sources.append(header_node.get_text(" ", strip=True))
    location_sources.extend(fields[key] for key in ("Job Locations", "Location : Location", "Additional Locations")
                            if fields.get(key))
    locations = []
    for source in location_sources:
        for part in (normalized(x) for x in re.split(r"[;|]", source)):
            if part and part not in locations:
                locations.append(part)

    sections = _section_rows(content)
    labels = [row["label"] for row in sections]
    if "Responsibilities" not in labels:
        raise ValueError(f"MaxLinear detail {expected_job_id} publishes no Responsibilities section")
    if any(label not in SECTION_LABELS for label in labels):
        raise ValueError(f"MaxLinear detail {expected_job_id} has an uninspected section: {labels}")
    description_html = "\n".join(
        f"<h2>{escape(row['label'])}</h2>\n{row['html']}" for row in sections
    )
    description_text = text_html(description_html)

    scripts = soup.select('script[type="application/ld+json"]')
    if len(scripts) != 1:
        raise ValueError(f"MaxLinear detail {expected_job_id} carries {len(scripts)} JobPosting blocks")
    try:
        data = json.loads(scripts[0].get_text())
    except ValueError as exc:
        raise ValueError(f"MaxLinear detail {expected_job_id} JSON-LD is invalid: {exc}") from exc
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        raise ValueError(f"MaxLinear detail {expected_job_id} has no JobPosting JSON-LD")
    if normalized(data.get("title")) != title:
        raise ValueError(f"MaxLinear detail {expected_job_id} JSON-LD title differs from the page")
    organization = data.get("hiringOrganization") or {}
    employer = normalized(organization.get("name")) if isinstance(organization, dict) else ""
    if employer != "MaxLinear, Inc.":
        raise ValueError(f"MaxLinear detail {expected_job_id} employer changed: {employer!r}")
    jsonld_url = urlsplit(data.get("url") or "")
    jsonld_match_route = JOB_PATH.fullmatch(jsonld_url.path)
    if (jsonld_url.netloc != parts.netloc or jsonld_match_route is None
            or jsonld_match_route.group(1) != expected_job_id):
        raise ValueError(f"MaxLinear detail {expected_job_id} JSON-LD url differs from its page")
    jsonld_description = data.get("description") or ""
    jsonld_text = text_html(jsonld_description)
    jsonld_match = bool(jsonld_text) and space_key(jsonld_text) == space_key(description_text)
    warnings = [] if jsonld_match else ["description_jsonld_mismatch"]

    apply_urls = [a["href"] for a in content.select("a[href]") if "mode=apply" in a["href"]]
    if len(apply_urls) != 1:
        raise ValueError(f"MaxLinear detail {expected_job_id} publishes {len(apply_urls)} apply links")
    apply_url = apply_urls[0]
    apply_parts = urlsplit(apply_url)
    apply_match = JOB_PATH.fullmatch(apply_parts.path)
    if apply_parts.netloc != parts.netloc or apply_match is None or apply_match.group(1) != expected_job_id:
        raise ValueError(f"MaxLinear detail {expected_job_id} apply link changed: {apply_url}")

    observed = observed_at or datetime.now(timezone.utc)
    posted_date, posted_source = source_posted_date(data.get("datePosted"), observed)
    raw_locations = data.get("jobLocation")
    if isinstance(raw_locations, dict):
        raw_locations = [raw_locations]
    addresses = []
    for place in raw_locations or []:
        address = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            addresses.append(_address(address))
    locations_out: list[dict] = []
    for index, address in enumerate(addresses):
        row = dict(address)
        if index < len(locations):
            row["location"] = locations[index]
        elif address.get("city"):
            row["location"] = address["city"] + (f", {address['region']}" if address.get("region") else "")
        locations_out.append(row or {"location": None})
    for extra in locations[len(addresses):]:
        locations_out.append({"location": extra})
    if not locations_out:
        locations_out = [{"location": location} for location in locations]

    section_text = {row["label"]: row["text"] for row in sections}
    salary = _salary(section_text.get("Compensation and Benefits", ""))
    if "Compensation and Benefits" in section_text and salary is None:
        salary = {"source_section": "Compensation and Benefits",
                  "text": normalized(section_text["Compensation and Benefits"]), "ranges": []}
    return {
        "portal": portal,
        "job_id": expected_job_id,
        "slug": detail_slug,
        "canonical_url": f"{PORTALS[portal]}{parts.path}",
        "title": title,
        "external_id": external_id,
        "header_fields": fields,
        "category": fields.get("Category"),
        "employment_type": fields.get("Type"),
        "locations": locations_out,
        "sections": [{"label": row["label"], "chars": len(row["text"])} for row in sections],
        "description_html": description_html,
        "description_text": description_text,
        "description_chars": len(normalized(description_text)),
        "jsonld_match": jsonld_match,
        "jsonld_description_sha256": sha256(normalized(jsonld_text).encode()).hexdigest(),
        "employer": employer,
        "occupational_category": data.get("occupationalCategory"),
        "jsonld_employment_type": data.get("employmentType"),
        "posted_date": posted_date,
        "posted_date_raw": data.get("datePosted") if posted_date else None,
        "jsonld_date_posted": data.get("datePosted"),
        "posted_date_source": posted_source,
        "valid_through": data.get("validThrough"),
        "jsonld_job_locations": addresses,
        "apply_url": apply_url,
        "salary": salary,
        "responsibilities": section_text.get("Responsibilities"),
        "qualifications": section_text.get("Qualifications"),
        "warnings": warnings,
    }


class MaxlinearTechnologies(Adapter):
    """Collect every posting visible on MaxLinear's two published iCIMS portals."""

    async def enumerate_listing(self) -> dict:
        cards: list[dict] = []
        portal_reports: dict[str, dict] = {}
        for portal, origin in PORTALS.items():
            page = 0
            total_pages = None
            portal_cards: list[dict] = []
            pages: list[int] = []
            while True:
                if not self.page_guard():
                    raise ValueError("MaxLinear listing page budget exhausted")
                route = LISTING_FIRST if page == 0 else LISTING_PAGE.format(page=page)
                response = await self.http.get(origin + route)
                parsed = parse_listing(response.text, str(response.url))
                if parsed["portal"] != portal:
                    raise ValueError(f"MaxLinear listing portal changed: {response.url}")
                if parsed["page_no"] != page + 1:
                    raise ValueError(f"MaxLinear listing returned page {parsed['page_no']} for request page {page + 1}")
                if total_pages is None:
                    total_pages = parsed["total_pages"]
                elif parsed["total_pages"] != total_pages:
                    raise ValueError("MaxLinear pagination total changed during enumeration")
                portal_cards.extend(parsed["cards"])
                pages.append(parsed["page_no"])
                if page + 1 >= total_pages:
                    break
                page += 1
            portal_reports[portal] = {
                "origin": origin,
                "listing_url": origin + LISTING_FIRST,
                "pages": len(pages),
                "total_pages": total_pages,
                "cards": len(portal_cards),
                "first_job_id": portal_cards[0]["job_id"] if portal_cards else None,
                "last_job_id": portal_cards[-1]["job_id"] if portal_cards else None,
            }
            cards.extend(portal_cards)

        seen: set[tuple[str, str]] = set()
        for card in cards:
            key = (card["portal"], card["job_id"])
            if key in seen:
                raise ValueError(f"MaxLinear listing repeats a posting: {key}")
            seen.add(key)
        urls = [card["url"] for card in cards]
        if len(set(urls)) != len(urls):
            raise ValueError("MaxLinear listing repeats a job URL")
        return {"cards": cards, "portals": portal_reports}

    async def fetch_detail(self, job):
        card = self._cards[job.url]
        response = await self.http.get(card["url"] + "?in_iframe=1")
        parsed = parse_detail(
            response.text,
            str(response.url),
            expected_portal=card["portal"],
            expected_job_id=card["job_id"],
            expected_slug=card["slug"],
            expected_external_id=card["external_id"],
            expected_title=card["title"],
        )
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = (
            "iCIMS detail page: h2 section labels + .iCIMS_InfoMsg_Job section bodies; "
            "must equal the page's JobPosting JSON-LD description (whitespace-insensitive)"
        )
        job.description_source_url = str(response.url)
        job.description_verified = parsed["jsonld_match"]
        job.apply_url = parsed["apply_url"]
        job.locations = parsed["locations"]
        job.department = parsed["category"]
        job.employment_type = parsed["employment_type"]
        job.employer = parsed["employer"]
        job.date_posted = parsed["posted_date"]
        job.date_posted_raw = parsed["posted_date_raw"]
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.language = "en"
        job.warnings.extend(parsed["warnings"])
        if not parsed["jsonld_match"]:
            # Job.validate treats this as a blocking provenance warning.
            job.warnings.append("description_not_full_field")
        job.warnings = list(dict.fromkeys(job.warnings))
        job.raw = {
            **(job.raw or {}),
            "icims_job_id": parsed["job_id"],
            "source_id": parsed["external_id"],
            "portal": parsed["portal"],
            "canonical_url": parsed["canonical_url"],
            "detail_url": str(response.url),
            "detail_sections": parsed["sections"],
            "detail_header_fields": parsed["header_fields"],
            "posted_date_observation": parsed["posted_date_source"],
            "jsonld_date_posted": parsed["jsonld_date_posted"],
            "jsonld_employment_type": parsed["jsonld_employment_type"],
            "jsonld_occupational_category": parsed["occupational_category"],
            "jsonld_valid_through": parsed["valid_through"],
            "jsonld_job_locations": parsed["jsonld_job_locations"],
            "jsonld_description_sha256": parsed["jsonld_description_sha256"],
        }
        return job

    async def _check_featured(self, featured: list[dict], membership: set[tuple[str, str]]) -> list[dict]:
        rows = []
        for item in featured:
            row = dict(item)
            row["in_listing"] = (item["portal"], item["job_id"]) in membership
            if row["in_listing"]:
                row["detail_state"] = "listed"
                rows.append(row)
                continue
            try:
                response = await self.http.get(item["url"] + "?in_iframe=1")
            except FetchError as exc:
                status = None
                match = re.search(r"HTTP (\d{3})", str(exc))
                if match:
                    status = int(match.group(1))
                row["http_status"] = status
                row["detail_state"] = "closed" if status in (404, 410) else "fetch_failed"
                row["reason"] = str(exc)[:200]
            else:
                row["http_status"] = response.status_code
                try:
                    parse_detail(
                        response.text,
                        str(response.url),
                        expected_portal=item["portal"],
                        expected_job_id=item["job_id"],
                        expected_slug=item["slug"],
                    )
                except ValueError:
                    row["detail_state"] = "unavailable"
                else:
                    row["detail_state"] = "live_outside_listing"
                    self.result.errors.append(
                        f"featured job {item['portal']}/{item['job_id']} is live but absent from the portal listing"
                    )
            rows.append(row)
        return rows

    async def run(self):
        entry_started = time.monotonic()
        response = await self.http.get(OFFICIAL_ENTRY)
        entry = parse_official_entry(response.text, str(response.url))
        self.result.timings["official_entry_seconds"] = max(0.0, time.monotonic() - entry_started)
        self.result.metadata["official_entry"] = {
            "url": OFFICIAL_ENTRY,
            "final_url": str(response.url),
            "http_status": response.status_code,
            "title": entry["title"],
            "portal_links": entry["portal_links"],
            "region_options": entry["region_options"],
            "featured": entry["featured"],
        }

        listing_started = time.monotonic()
        listing = await self.enumerate_listing()
        self.result.timings["listing_seconds"] = max(0.0, time.monotonic() - listing_started)
        self.result.expected_total = len(listing["cards"])
        self.result.metadata["portal_listings"] = listing["portals"]
        self.result.metadata["membership"] = {
            "cards_total": len(listing["cards"]),
            "portals": {portal: report["cards"] for portal, report in listing["portals"].items()},
            "pages_total": sum(report["pages"] for report in listing["portals"].values()),
        }
        self._cards = {card["url"]: card for card in listing["cards"]}
        for card in listing["cards"]:
            job = self.job(
                card,
                title=card["title"],
                url=card["url"],
                job_id=card["job_id"],
                requisition_id=card["external_id"],
                department=card["category"],
                locations=[{"location": card["location"]}] if card["location"] else [],
            )
            job.raw = {
                "listing": {
                    "portal": card["portal"],
                    "external_id": card["external_id"],
                    "category": card["category"],
                    "location": card["location"],
                    "listing_url": card["listing_url"],
                    "preview_text": card["preview_text"],
                }
            }
            self.add(job)

        await self.hydrate(self.result.jobs, self.fetch_detail)

        featured_started = time.monotonic()
        membership = {(card["portal"], card["job_id"]) for card in listing["cards"]}
        featured_rows = await self._check_featured(entry["featured"], membership)
        self.result.metadata["featured_jobs"] = featured_rows
        self.result.timings["featured_check_seconds"] = max(0.0, time.monotonic() - featured_started)

        recheck_started = time.monotonic()
        recheck = await self.enumerate_listing()
        after = {(card["portal"], card["job_id"]): card for card in recheck["cards"]}
        before = {(card["portal"], card["job_id"]): card for card in listing["cards"]}
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        moved = sorted(key for key in set(after) & set(before)
                       if after[key]["url"] != before[key]["url"])
        self.result.timings["membership_recheck_seconds"] = max(0.0, time.monotonic() - recheck_started)
        self.result.metadata["membership_recheck"] = {
            "cards_total": len(recheck["cards"]),
            "added": added,
            "removed": removed,
            "changed_url": moved,
        }
        if added or removed or moved:
            self.result.errors.append("listing_membership_changed_during_run")
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True
        if self.result.expected_total != len(self.result.jobs):
            self.result.errors.append("expected_total_mismatch")
            self.result.listing_complete = False
        self.result.metadata["posted_date_policy"] = (
            "International portal publishes genuine datePosted (clean 04:00/05:00Z); stored as "
            "date_posted. US portal publishes no posted date; iCIMS fills datePosted with fetch "
            "time - 2 years, so date_posted stays null and the raw value is labelled "
            "request_derived_template. validThrough is generated, never used as a date."
        )
        self.result.metadata["verification_revision"] = VERIFICATION_REVISION
        self.result.metadata["company_scope_answer"] = (
            "MaxLinear's own careers page publishes exactly these two iCIMS portals (US and "
            "International) as its full job search destinations; membership is every card on "
            "their unfiltered paginations."
        )
        return self.result
