"""Nutanix's own careers site: global HTML listing, Jobvite feed and JobPosting pages.

Journey inspected on 2026-11-07:
  https://www.nutanix.com/company/careers
    -> 302 https://careers.nutanix.com/en/ (Nutanix's own site)
    -> https://careers.nutanix.com/en/jobs/ (server-rendered, 249 unfiltered jobs)
The company publishes the same board as an RSS/XML feed and in its sitemap; both are
used only to reconcile the listing membership. Nothing is guessed: the feed, sitemap
and every detail URL must agree exactly with the 13 listing pages, and the apply link
is taken from the detail page (never constructed for submission).
"""
from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

OFFICIAL_ENTRY = "https://www.nutanix.com/company/careers"
CAREERS_ORIGIN = "https://careers.nutanix.com"
CAREERS_HOME = CAREERS_ORIGIN + "/en/"
LISTING_URL = CAREERS_ORIGIN + "/en/jobs/"
FEED_URL = CAREERS_ORIGIN + "/en/jobs/xml/?rss=true"
SITEMAP_URL = CAREERS_ORIGIN + "/sitemap.xml"
PAGE_SIZE = 20
JOB_PATH = re.compile(r"^/en/jobs/([^/]+)/([^/]+)/$")
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
APPLY_HOST = "app.jobvite.com"
# The site answers a non-browser User-Agent with page 1 for every ?page=N request
# (HTTP 200, no challenge/403/429). The same public pages paginate correctly for a
# desktop browser, which is what the inspected UI uses; the other adapters in this
# repository also send this standard desktop UA. No CAPTCHA or access control is
# bypassed, and any 429 still stops the run through the shared HTTP helper.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
APPLY_QUERY = {"k": ["Apply"], "c": ["qKr9VfwZ"]}
EEO_FOOTER_SHA256 = "40726f975b91139a4facf5fbd8e43af48adec5dd0de64044c5a8574f4bd34366"
EEO_FOOTER = (
    "We're an Equal Opportunity Employer Nutanix is an Equal Employment Opportunity "
    "and (in the U.S.) an Affirmative Action employer. Qualified applicants are "
    "considered for employment opportunities without regard to race, color, religion, "
    "sex, sexual orientation, gender identity or expression, national origin, age, "
    "marital status, protected veteran status, disability status or any other category "
    "protected by applicable law. We hire and promote individuals solely on the basis "
    "of qualifications for the job to be filled. We strive to foster an inclusive "
    "working environment that enables all our Nutants to be themselves and to do great "
    "work in a safe and welcoming environment, free of unlawful discrimination, "
    "intimidation or harassment. As part of this commitment, we will ensure that "
    "persons with disabilities are provided reasonable accommodations. If you need a "
    "reasonable accommodation, please let us know by contacting <email> ."
)
EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|\[email\s*protected\]", re.I
)
DISPLAY_COUNT = re.compile(
    r"Displaying\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+matching jobs"
)
ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

RESPONSIBILITY_PREFIXES = (
    "your role", "roles & responsibilities", "roles and responsibilities",
    "responsibilit", "key responsibilities", "core responsibilities",
    "what you will do", "what you'll do", "what you’ll do",
    "your day-to-day responsibilities", "major duties", "main duties",
    "duties and responsibilities", "job responsibilities", "key duties",
    "role overview", "the role",
)
QUALIFICATION_PREFIXES = (
    "what you will bring", "what you'll bring", "what you’ll bring",
    "what will you bring", "what you need to bring", "what you bring",
    "qualifications", "required qualifications", "preferred qualifications",
    "minimum qualifications", "requirements", "desired qualification",
    "desired qualifications", "about you", "experience & qualifications",
    "experience and education", "required profile", "required skills",
    "nice to haves", "technical requirement", "candidate profile", "who you are",
    "the ideal candidate", "must have", "good to have", "required experience",
    "preferred experience", "mandatory skills", "skills & experience",
    "skills and experience",
)
OTHER_HEADING_PREFIXES = (
    "the opportunity", "about the team", "work arrangement", "pay transparency",
    "how we work", "about nutanix", "benefits", "highlighted benefits",
    "education & certifications", "preferred certifications", "location & work shifts",
    "the team", "about us", "additional information", "compensation",
    "equal opportunity employer", "what success looks like", "tech stack",
    "why nutanix", "life at nutanix", "role location",
    "learn more about the technology", "job summary",
)
SALARY_MARK = re.compile(
    r"(?:pay range|salary range|compensation range|base salary|ote range|pay transparency)", re.I
)
CURRENCY_TOKEN = re.compile(
    r"(?:USD|CAD|EUR|GBP|SEK|SGD|INR|AUD|JPY|CHF|NZD)\b|[$€£]", re.I
)
RANGE = re.compile(
    r"between\s+(?:([A-Z]{3})\s*)?[€$£]?\s*([\d][\d,]*(?:\.\d+)?)\s+and\s+"
    r"(?:([A-Z]{3})\s*)?[€$£]?\s*([\d][\d,]*(?:\.\d+)?)",
    re.I,
)
MINIMUM = re.compile(
    r"starting from\s*(?:([A-Z]{3})\s*)?([€$£])?\s*([\d][\d,]*(?:\.\d+)?)", re.I
)
SYMBOL_CURRENCY = {"€": "EUR", "£": "GBP"}
COUNTRY_CURRENCY = {
    "United States": "USD", "Canada": "CAD", "Australia": "AUD",
    "Singapore": "SGD", "India": "INR", "Japan": "JPY",
}


def normalized_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def masked_email(value: str) -> str:
    return EMAIL.sub("<email>", value or "")


def compact_body_sha256(value: str) -> str:
    """Whitespace-free hash used to compare stored and rendered bodies."""
    return sha256(re.sub(r"\s+", "", value or "").encode()).hexdigest()


def heading_key(value: str) -> str:
    key = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return key.rstrip(":").strip()


def classify_heading(value: str) -> str | None:
    key = heading_key(value)
    if not key or len(key) > 110:
        return None
    if any(key.startswith(prefix) for prefix in RESPONSIBILITY_PREFIXES):
        return "responsibility"
    if any(key.startswith(prefix) for prefix in QUALIFICATION_PREFIXES):
        return "qualification"
    if any(key.startswith(prefix) for prefix in OTHER_HEADING_PREFIXES):
        return "other"
    if "(your role" in key or "(key responsibilities" in key:
        return "responsibility"
    if "(requirements" in key or "(candidate profile" in key or "(who you are" in key:
        return "qualification"
    return None


def _section(description_html: str, kind: str) -> str | None:
    soup = BeautifulSoup(description_html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if classify_heading(line) != kind:
            continue
        block: list[str] = []
        for following in lines[index + 1:]:
            if classify_heading(following) is not None:
                break
            block.append(following)
        value = "\n".join(block).strip()
        if len(re.sub(r"\s+", " ", value)) >= 40:
            return value
    return None


def description_sections(description_html: str) -> tuple[str | None, str | None]:
    """Verbatim excerpts only; never synthesize qualification/responsibility text."""
    qualifications = _section(description_html, "qualification")
    responsibilities = _section(description_html, "responsibility")
    return qualifications, responsibilities


def _amount(value: str):
    cleaned = value.replace(",", "")
    return float(cleaned) if "." in cleaned else int(cleaned)


def salary_from_description(description_html: str, country: str | None = None) -> dict | None:
    soup = BeautifulSoup(description_html or "", "lxml")
    paragraphs: list[str] = []
    for node in soup.find_all(["p", "li"]):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if (text and SALARY_MARK.search(text) and CURRENCY_TOKEN.search(text)
                and re.search(r"\d", text) and text not in paragraphs):
            paragraphs.append(text)
    if not paragraphs:
        return None
    source_text = "\n".join(paragraphs)
    value = {
        "source_text": source_text,
        "source_field": "JobPosting.description",
        "kind": None,
        "currency": None,
        "minimum": None,
        "maximum": None,
        "unit": None,
    }
    range_match = RANGE.search(source_text)
    if range_match:
        first, second = range_match.group(2), range_match.group(4)
        currency = (range_match.group(1) or range_match.group(3) or "").upper() or None
        if not currency:
            symbol = re.search(r"[€$£]", source_text)
            currency = SYMBOL_CURRENCY.get(symbol.group(0)) if symbol else None
            if currency is None and symbol and symbol.group(0) == "$":
                currency = COUNTRY_CURRENCY.get(country or "", "USD")
        value.update(kind="range", currency=currency,
                     minimum=_amount(first), maximum=_amount(second))
    else:
        minimum_match = MINIMUM.search(source_text)
        if minimum_match:
            currency = (minimum_match.group(1) or "").upper() or None
            symbol = minimum_match.group(2)
            if not currency and symbol:
                currency = SYMBOL_CURRENCY.get(symbol)
                if currency is None and symbol == "$":
                    currency = COUNTRY_CURRENCY.get(country or "", "USD")
            value.update(kind="minimum", currency=currency,
                         minimum=_amount(minimum_match.group(3)))
        else:
            value["kind"] = "other"
    if re.search(r"per year|per annum|/\s*year|annual", source_text, re.I):
        value["unit"] = "YEAR"
    return value


def parse_entry(html: str, final_url: str) -> dict:
    parsed = urlsplit(final_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.nutanix.com" or parsed.path != "/en/":
        raise ValueError(f"Nutanix official careers entry no longer lands on its own careers home: {final_url}")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Nutanix Careers" not in title:
        raise ValueError("Nutanix careers home title changed")
    jobs_links = {
        urljoin(final_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
    }
    listing = LISTING_URL
    if listing not in jobs_links:
        raise ValueError("Nutanix careers home no longer links its own /en/jobs/ listing")
    organization = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Organization":
            organization = data.get("name")
    if organization != "Nutanix":
        raise ValueError("Nutanix careers home organization identity changed")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": final_url,
        "title": title,
        "jobs_url": listing,
        "organization": organization,
    }


def parse_listing_page(html: str, page: int, page_size: int = PAGE_SIZE) -> dict:
    if page < 1:
        raise ValueError("Nutanix listing page numbers start at 1")
    soup = BeautifulSoup(html, "lxml")
    counter = soup.select_one(".job-count")
    match = DISPLAY_COUNT.search(counter.get_text(" ", strip=True)) if counter else None
    if not match:
        raise ValueError(f"Nutanix listing page {page} has no displayed result counter")
    first, last, total = (int(value) for value in match.groups())
    expected_first = (page - 1) * page_size + 1
    expected_last = min(page * page_size, total)
    expected_pages = max(1, (total + page_size - 1) // page_size)
    if page > expected_pages or (first, last) != (expected_first, expected_last):
        raise ValueError(f"Nutanix listing page {page} counter disagrees with {total} total: {(first, last, total)}")

    rows, seen = [], set()
    for card in soup.select(".job-listing .card-job"):
        reference = card.select_one(".reference-number")
        anchor = card.select_one("h2.card-title a.js-view-job[href]")
        location = card.select_one(".job-meta-location")
        team = card.select_one(".job-meta-team")
        if not all((reference, anchor, location, team)):
            raise ValueError(f"Nutanix listing page {page} has a job card missing published fields")
        href = anchor.get("href", "")
        target = urlsplit(urljoin(CAREERS_ORIGIN + "/en/jobs/", href))
        path = JOB_PATH.match(target.path)
        if target.netloc != "careers.nutanix.com" or not path:
            raise ValueError(f"Nutanix listing page {page} card link left the site: {href}")
        identity = path.group(1)
        if not JOB_ID.fullmatch(identity) or identity in seen:
            raise ValueError(f"Nutanix listing page {page} repeats or invents a job id: {identity}")
        seen.add(identity)
        rows.append({
            "id": identity,
            "slug": path.group(2),
            "url": f"{CAREERS_ORIGIN}{target.path}",
            "title": anchor.get_text(" ", strip=True),
            "reference_number": reference.get_text(" ", strip=True),
            "location": location.get_text(" ", strip=True),
            "team": team.get_text(" ", strip=True),
            "card_html_sha256": sha256(str(card).encode()).hexdigest(),
        })
    if len(rows) != last - first + 1:
        raise ValueError(f"Nutanix listing page {page} rendered {len(rows)} cards for {first}-{last}")

    previous = soup.select_one(".pagination a[rel~='prev']")
    following = soup.select_one(".pagination a[rel~='next']")
    if page < expected_pages:
        if following is None or parse_qs(urlsplit(following.get("href", "")).query).get("page") != [str(page + 1)]:
            raise ValueError(f"Nutanix listing page {page} no longer paginates to page {page + 1}")
    elif following is not None:
        raise ValueError(f"Nutanix final listing page {page} unexpectedly offers a next page")
    if page > 1 and previous is None:
        raise ValueError(f"Nutanix listing page {page} unexpectedly offers no previous page")
    return {
        "page": page,
        "page_size": page_size,
        "first": first,
        "last": last,
        "total": total,
        "pages": expected_pages,
        "rows": rows,
        "next_page": page + 1 if page < expected_pages else None,
    }


def parse_feed(xml_text: str) -> dict:
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Nutanix company feed is not valid XML: {exc}") from exc
    if root.tag != "source":
        raise ValueError("Nutanix company feed root changed")
    publisher = root.findtext("publisher")
    publisher_url = root.findtext("publisherurl")
    if publisher != "Nutanix" or publisher_url != "https://www.nutanix.com/":
        raise ValueError("Nutanix company feed publisher changed")
    jobs, identities, requisitions = [], set(), set()
    for node in root.findall("job"):
        row = {child.tag: (child.text or "").strip() for child in node}
        identity = row.get("apijobid", "")
        requisition = row.get("requisitionid", "")
        if (not JOB_ID.fullmatch(identity) or not requisition
                or identity in identities or requisition in requisitions):
            raise ValueError("Nutanix company feed repeats or misses a job identity")
        target = urlsplit(row.get("url", ""))
        path = JOB_PATH.match(target.path)
        if (target.scheme != "https" or target.netloc != "careers.nutanix.com"
                or not path or path.group(1) != identity):
            raise ValueError(f"Nutanix feed job {identity} has a non-company URL: {row.get('url')}")
        title = row.get("title")
        if not title or not row.get("description") or len(normalized_text(row["description"])) < 100:
            raise ValueError(f"Nutanix feed job {identity} is missing its title or full description")
        if ISO_DATE.match(row.get("date", "")) is None:
            raise ValueError(f"Nutanix feed job {identity} has no usable posting date")
        identities.add(identity)
        requisitions.add(requisition)
        jobs.append(row)
    if not jobs:
        raise ValueError("Nutanix company feed contains no jobs")
    return {
        "publisher": publisher,
        "publisher_url": publisher_url,
        "last_build_date": root.findtext("lastBuildDate"),
        "jobs": jobs,
        "ids": [row["apijobid"] for row in jobs],
    }


def parse_sitemap(xml_text: str) -> dict:
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Nutanix sitemap is not valid XML: {exc}") from exc
    rows = {}
    for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
        target = urlsplit((loc.text or "").strip())
        path = JOB_PATH.match(target.path)
        if target.netloc == "careers.nutanix.com" and path:
            rows[path.group(1)] = f"{CAREERS_ORIGIN}{target.path}"
    if not rows:
        raise ValueError("Nutanix sitemap exposes no job URLs")
    return {"ids": list(rows), "urls": rows, "job_url_count": len(rows)}


def _locations(value) -> list[dict]:
    entries = value if isinstance(value, list) else [value]
    locations = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Nutanix JobPosting jobLocation is not an object")
        address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
        locations.append({
            "name": (entry.get("name") or "").strip() or None,
            "city": (address.get("addressLocality") or "").strip() or None,
            "region": (address.get("addressRegion") or "").strip() or None,
            "country": (address.get("addressCountry") or "").strip() or None,
            "postal_code": (address.get("postalCode") or "").strip() or None,
            "street_address": (address.get("streetAddress") or "").strip() or None,
        })
    if not locations or not all(location.get("name") for location in locations):
        raise ValueError("Nutanix JobPosting jobLocation has no named location")
    return locations


def parse_detail(html: str, url: str, feed_row: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        text = script.string or script.get_text() or ""
        if '"@type"' not in text or '"JobPosting"' not in text:
            continue
        try:
            data = json.loads(text.strip())
        except ValueError as exc:
            raise ValueError(f"Nutanix job {feed_row['apijobid']} has unparseable JobPosting JSON-LD") from exc
        if data.get("@type") == "JobPosting":
            postings.append(data)
    if len(postings) != 1:
        raise ValueError(f"Nutanix job {feed_row['apijobid']} no longer exposes exactly one JobPosting object")
    posting = postings[0]
    identity = feed_row["apijobid"]
    title = posting.get("title") or ""
    if (normalized_text(title) != normalized_text(feed_row.get("title"))
            or normalized_text(title) != normalized_text(soup.select_one("h1.hero-heading").get_text(" ", strip=True) if soup.select_one("h1.hero-heading") else "")):
        raise ValueError(f"Nutanix job {identity} title disagrees between feed and page")
    if posting.get("identifier") != feed_row.get("requisitionid"):
        raise ValueError(f"Nutanix job {identity} JobPosting identifier changed")
    date_posted = posting.get("datePosted") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted) or date_posted != feed_row.get("date", "")[:10]:
        raise ValueError(f"Nutanix job {identity} posting date disagrees between feed and page")
    organization = (posting.get("hiringOrganization") or {}).get("name")
    if organization != "Nutanix":
        raise ValueError(f"Nutanix job {identity} hiring organization changed: {organization}")
    if unquote(urlsplit(posting.get("url", "")).path) != unquote(urlsplit(url).path):
        raise ValueError(f"Nutanix job {identity} canonical page URL changed")
    page_identity = soup.select_one("meta[name='JobIdentifier']")
    if page_identity is None or page_identity.get("content") != identity:
        raise ValueError(f"Nutanix job {identity} page identifier changed")

    hero = soup.select_one(".job-hero-meta")
    hero_items = hero.find_all("li", recursive=False) if hero else []
    team = hero_items[0].get_text(" ", strip=True) if hero_items else None
    if team != feed_row.get("category"):
        raise ValueError(f"Nutanix job {identity} team disagrees between feed and page: {team}")

    applies = {
        urljoin(url, anchor.get("href", ""))
        for anchor in soup.select("a.js-apply-now[href]")
    }
    if len(applies) != 1:
        raise ValueError(f"Nutanix job {identity} no longer exposes one published apply link")
    apply_url = applies.pop()
    apply_parsed = urlsplit(apply_url)
    if apply_parsed.scheme != "https" or apply_parsed.netloc != APPLY_HOST:
        raise ValueError(f"Nutanix job {identity} apply link left its published Jobvite route")
    apply_query = parse_qs(apply_parsed.query)
    for key, expected in APPLY_QUERY.items():
        if apply_query.get(key) != expected:
            raise ValueError(f"Nutanix job {identity} apply link {key} changed")
    if apply_query.get("j") != [feed_row["requisitionid"]]:
        raise ValueError(f"Nutanix job {identity} apply link requisition changed")

    description_html = posting.get("description") or ""
    if normalized_text(description_html) != normalized_text(feed_row.get("description")):
        raise ValueError(f"Nutanix job {identity} JobPosting description disagrees with the company feed")
    body = soup.select_one("#job-desc")
    if body is None:
        raise ValueError(f"Nutanix job {identity} has no published job description block")
    page_body = normalized_text(body.decode_contents())
    role_body = normalized_text(description_html)
    expected_page = masked_email(role_body) + " " + EEO_FOOTER
    if masked_email(page_body) != expected_page:
        raise ValueError(f"Nutanix job {identity} rendered body is not its full JobPosting description plus the standard EEO footer")
    if sha256(EEO_FOOTER.encode()).hexdigest() != EEO_FOOTER_SHA256:
        raise ValueError("Nutanix EEO footer constant is inconsistent")
    if len(role_body) < 100:
        raise ValueError(f"Nutanix job {identity} description is empty or truncated")

    locations = _locations(posting.get("jobLocation"))
    if not any(feed_row.get("city") in (location.get("city") or "") or
               (location.get("name") or "").startswith(feed_row.get("city", "") + ",")
               for location in locations):
        raise ValueError(f"Nutanix job {identity} feed city is absent from the JobPosting locations")

    qualifications, responsibilities = description_sections(description_html)
    salary = salary_from_description(description_html, feed_row.get("country"))
    return {
        "posting": posting,
        "title": title,
        "date_posted": date_posted,
        "date_updated": None,
        "locations": locations,
        "team": team,
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": role_body,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "salary": salary,
        "page_body_sha256": compact_body_sha256(page_body),
        "page_body_chars": len(page_body),
        "role_body_sha256": compact_body_sha256(role_body),
        "role_body_chars": len(role_body),
        "eeo_footer_sha256": sha256(EEO_FOOTER.encode()).hexdigest(),
        "eeo_footer_chars": len(EEO_FOOTER),
    }


class Nutanix(Adapter):
    """Follow Nutanix's own unfiltered careers listing and its exact job pages."""

    VERIFICATION_REVISION = "nutanix.native-careers-global-listing-and-jobposting-detail.1"

    async def _listing_pass(self, label: str) -> dict:
        started = time.perf_counter()
        pages, rows = [], []
        total = None
        for page in range(1, 1000):
            if not self.page_guard():
                raise ValueError("Nutanix listing page budget exhausted")
            url = f"{LISTING_URL}?pagesize={PAGE_SIZE}&page={page}"
            response = await self.http.get(url, headers=BROWSER_HEADERS)
            if str(response.url) != url:
                raise ValueError(f"Nutanix {label} listing page {page} redirected away from its source")
            parsed = parse_listing_page(response.text, page)
            if total is None:
                total = parsed["total"]
            elif parsed["total"] != total:
                raise ValueError(f"Nutanix {label} listing total changed during the pass")
            pages.append({"page": page, "url": url, "first": parsed["first"],
                          "last": parsed["last"], "rows": len(parsed["rows"]),
                          "history": [str(item.url) for item in response.history]})
            rows.extend(parsed["rows"])
            if parsed["next_page"] is None:
                break
        if not rows or total is None:
            raise ValueError(f"Nutanix {label} listing exposed no jobs")
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)) or len(ids) != total:
            raise ValueError(f"Nutanix {label} listing membership does not reconcile: {len(ids)}/{total}")
        expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if len(pages) != expected_pages:
            raise ValueError(f"Nutanix {label} listing page count disagrees: {len(pages)}/{expected_pages}")
        return {
            "label": label,
            "rows": rows,
            "ids": ids,
            "total": total,
            "pages": pages,
            "first_page_rows": pages[0]["first"],
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _fetch_feed(self) -> dict:
        response = await self.http.get(FEED_URL, headers=BROWSER_HEADERS)
        if str(response.url) != FEED_URL:
            raise ValueError("Nutanix company feed redirected away from its published URL")
        parsed = parse_feed(response.text)
        parsed["url"] = FEED_URL
        return parsed

    async def _fetch_sitemap(self) -> dict:
        response = await self.http.get(SITEMAP_URL, headers=BROWSER_HEADERS)
        if str(response.url) != SITEMAP_URL:
            raise ValueError("Nutanix sitemap redirected away from its published URL")
        parsed = parse_sitemap(response.text)
        parsed["url"] = SITEMAP_URL
        return parsed

    def _reconcile(self, listing: dict, feed: dict, sitemap: dict) -> dict:
        listing_ids, feed_ids, sitemap_ids = listing["ids"], feed["ids"], sitemap["ids"]
        listing_set, feed_set, sitemap_set = set(listing_ids), set(feed_ids), set(sitemap_ids)
        details = {
            "listing_total": listing["total"],
            "listing_ids": len(listing_ids),
            "feed_ids": len(feed_ids),
            "sitemap_job_ids": len(sitemap_ids),
            "listing_pages": len(listing["pages"]),
            "page_size": PAGE_SIZE,
            "listing_order_equals_feed_order": listing_ids == feed_ids,
            "listing_ids_equal_feed_ids": listing_set == feed_set,
            "listing_ids_equal_sitemap_ids": listing_set == sitemap_set,
            "feed_ids_equal_sitemap_ids": feed_set == sitemap_set,
            "listing_only_ids": sorted(listing_set - feed_set)[:20],
            "feed_only_ids": sorted(feed_set - listing_set)[:20],
            "sitemap_only_ids": sorted(sitemap_set - listing_set)[:20],
        }
        if not (details["listing_order_equals_feed_order"]
                and details["listing_ids_equal_feed_ids"]
                and details["listing_ids_equal_sitemap_ids"]):
            raise ValueError(f"Nutanix listing/feed/sitemap membership disagreed: {details}")
        if listing["total"] != len(listing_ids) or len(feed_ids) != len(sitemap_ids):
            raise ValueError("Nutanix membership counts disagree between listing, feed and sitemap")
        return details

    async def run(self):
        started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY, headers=BROWSER_HEADERS)
        entry = parse_entry(entry_response.text, str(entry_response.url))
        entry["redirects"] = [str(item.url) for item in entry_response.history]
        self.result.metadata["official_entry"] = entry

        listing = await self._listing_pass("initial")
        feed = await self._fetch_feed()
        sitemap = await self._fetch_sitemap()
        reconciliation = self._reconcile(listing, feed, sitemap)
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - started), 6)
        self.result.expected_total = listing["total"]

        cards = {row["id"]: row for row in listing["rows"]}
        jobs = []
        for feed_row in feed["jobs"]:
            identity = feed_row["apijobid"]
            card = cards[identity]
            job = self.job(
                feed_row,
                title=feed_row["title"],
                url=feed_row["url"],
                job_id=identity,
                requisition_id=feed_row["requisitionid"],
                description="",
                method=None,
                description_url=feed_row["url"],
                apply_url=None,
                locations=[{"name": card["location"]}],
                department=feed_row.get("category") or None,
                team=feed_row.get("jobFamilySubgroup") or None,
                employment_type=feed_row.get("jobtype") or None,
                workplace_type=None,
                date_posted=feed_row["date"][:10],
                date_posted_raw=feed_row["date"],
                date_updated=None,
                salary=None,
                qualifications=None,
                responsibilities=None,
                employer={"name": feed_row.get("company"), "email": feed_row.get("email")},
                language="en",
            )
            job.raw = {
                "feed_row": feed_row,
                "listing_card": card,
                "posting_kind": ("talent_community"
                                  if feed_row.get("jobtype") == "Talent Community" else "posting"),
            }
            jobs.append(job)
        if [job.job_id for job in jobs] != listing["ids"]:
            raise ValueError("Nutanix jobs were not created in the listing's published order")

        await self.hydrate(jobs, self.detail)

        recheck_started = time.perf_counter()
        final_reconciliation = {"completed": False}
        try:
            final_listing = await self._listing_pass("final")
            final_feed = await self._fetch_feed()
            final_sitemap = await self._fetch_sitemap()
            final_reconciliation = self._reconcile(final_listing, final_feed, final_sitemap)
            final_reconciliation["completed"] = True
            final_reconciliation["final_listing_pages"] = final_listing["pages"]
            if (final_listing["ids"] != listing["ids"] or final_feed["ids"] != feed["ids"]
                    or final_sitemap["ids"] != sitemap["ids"]):
                self.result.errors.append("membership_changed_during_run")
                final_reconciliation["completed"] = False
        except Exception as exc:
            self.result.errors.append(f"final_membership_recheck_failed: {type(exc).__name__}: {exc}")
            final_reconciliation["error"] = f"{type(exc).__name__}: {exc}"
        self.result.timings["final_membership_recheck_seconds"] = round(
            max(0.0, time.perf_counter() - recheck_started), 6)
        for job in jobs:
            self.add(job)

        posting_kinds: dict[str, int] = {}
        for job in jobs:
            kind = job.raw.get("posting_kind", "posting")
            posting_kinds[kind] = posting_kinds.get(kind, 0) + 1
        self.result.metadata.update({
            "membership_reconciliation": reconciliation,
            "final_membership_reconciliation": final_reconciliation,
            "initial_listing_pages": listing["pages"],
            "final_listing_pages": final_reconciliation.get("final_listing_pages", []),
            "feed": {
                "url": FEED_URL,
                "publisher": feed["publisher"],
                "last_build_date": feed["last_build_date"],
                "jobs": len(feed["jobs"]),
            },
            "sitemap": {"url": SITEMAP_URL, "job_urls": sitemap["job_url_count"]},
            "posting_kinds": posting_kinds,
            "detail_pages_requested": len(jobs),
            "description_source": "JobPosting.description (rendered #job-desc minus the standard 934-char EEO footer)",
            "eeo_footer_sha256": EEO_FOOTER_SHA256,
            "apply_route": "app.jobvite.com CompanyJobs Careers.aspx (retained, never submitted)",
            "listing_url": LISTING_URL,
        })
        self.result.listing_complete = bool(
            reconciliation["listing_order_equals_feed_order"]
            and final_reconciliation.get("completed")
            and final_reconciliation.get("listing_order_equals_feed_order")
        )
        self.result.finish()
        return self.result

    async def detail(self, job):
        response = await self.http.get(job.url, headers=BROWSER_HEADERS)
        if str(response.url) != job.url:
            raise ValueError(f"Nutanix job {job.job_id} redirected away from its listed URL")
        parsed = parse_detail(response.text, job.url, job.raw["feed_row"])
        posting = parsed["posting"]
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_method = "jobposting_jsonld_description"
        job.description_source_url = job.url
        job.locations = parsed["locations"]
        job.apply_url = parsed["apply_url"]
        job.date_posted = parsed["date_posted"]
        job.date_updated = parsed["date_updated"]
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.employer = {
            "name": (posting.get("hiringOrganization") or {}).get("name"),
            "url": (posting.get("hiringOrganization") or {}).get("url"),
            "email": (posting.get("hiringOrganization") or {}).get("email"),
        }
        job.raw["detail"] = {
            "jsonld": {key: value for key, value in posting.items() if key != "description"},
            "page_identity": job.job_id,
            "team": parsed["team"],
            "apply_url": parsed["apply_url"],
            "page_body_sha256": parsed["page_body_sha256"],
            "page_body_chars": parsed["page_body_chars"],
            "role_body_sha256": parsed["role_body_sha256"],
            "role_body_chars": parsed["role_body_chars"],
            "eeo_footer_sha256": parsed["eeo_footer_sha256"],
            "eeo_footer_chars": parsed["eeo_footer_chars"],
            "description_matches_rendered_page": True,
            "feed_description_matches_jobposting": True,
        }
        return job
