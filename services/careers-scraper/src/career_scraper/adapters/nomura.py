"""Nomura's official careers journey: global careers site + TAL campus board.

Membership authority is Nomura's own careers platform.  The official entry
``https://www.nomura.com/careers/`` publishes two journeys:

* Early Careers -> the TAL ``Student Opportunities`` board (23 public
  student/talent-programme opportunities; the separate Events board is not a
  job listing and currently has no active events).
* Experienced Professionals -> ``careers.nomura.com`` (Radancy/TalentBrew over
  SuccessFactors), whose unfiltered site-wide search exposes 494 postings.
  The four regional Career Opportunities categories expose 492 of them; the
  remaining two Mumbai roles are exposed by the same site-wide search and the
  Nomura-brand search (486).  Eight of the 494 are published under the
  Instinet (7) and Nomura Greentech (1) brands in Nomura's own regional
  listings; they are retained with the brand recorded, never guessed.

Only the exact server-rendered listing pages, the site-wide search pages and
the exact listed job/detail URLs are requested.  The sitemap is a completeness
cross-check only.  No date is rendered on either listing or detail page, so
posting/update dates stay null; the TAL application deadline is a real deadline
and is retained separately.
"""
from __future__ import annotations

import re
import time
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

OFFICIAL_ENTRY = "https://www.nomura.com/careers/"
EARLY_CAREERS_ENTRY = "https://www.nomura.com/careers/early-careers/"
EXPERIENCED_ENTRY = "https://www.nomuraholdings.com/en/careers.html"
CAREERS_ORIGIN = "https://careers.nomura.com"
GLOBAL_SEARCH = CAREERS_ORIGIN + "/search/?q=&sortColumn=referencedate&sortDirection=desc"
BRAND_SEARCH = CAREERS_ORIGIN + "/Nomura/search/?q=&sortColumn=referencedate&sortDirection=desc"
SITEMAP = CAREERS_ORIGIN + "/sitemap.xml"
TAL_HOST = "nomuracampus.tal.net"
TAL_BOARD = ("https://nomuracampus.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/"
             "brand-4/xf-3348347fc789/candidate/jobboard/vacancy/1/adv/")
TAL_EVENTS = ("https://nomuracampus.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/"
              "brand-4/xf-3348347fc789/candidate/jobboard/vacancy/2/adv/")
TAL_OPP = re.compile(r"/candidate/so/pm/1/pl/1/opp/(\d+)-")
RADANCY_JOB = re.compile(r"^/(Nomura|Instinet|Greentech)/job/[^/]+/(\d+)/?$")
BRANDS = {"Nomura": "Nomura", "Instinet": "Instinet", "Greentech": "Nomura Greentech"}
REGIONAL_CATEGORIES = {
    "Americas": CAREERS_ORIGIN + "/Nomura/go/Career-Opportunities-Americas/9050800/",
    "Asia Pacific": CAREERS_ORIGIN + "/Nomura/go/Career-Opportunities-Asia-Pacific/9051000/",
    "Europe": CAREERS_ORIGIN + "/Nomura/go/Career-Opportunities-Europe/9051100/",
    "India": CAREERS_ORIGIN + "/Nomura/go/Career-Opportunities-India/9050900/",
}
COUNT_TEXT = re.compile(r"Results?\s+([\d,]+)\s*[–-]\s*([\d,]+)\s+of\s+([\d,]+)", re.I)
TAL_COUNT = re.compile(r"([\d,]+)\s+results?\s+match", re.I)
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_title(value: str | None) -> str:
    """Decode entities, collapse whitespace and trim (titles keep their case)."""
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _pagination_counts(soup: BeautifulSoup) -> tuple[int, int, int]:
    label = soup.select_one(".paginationLabel")
    if label is None:
        raise ValueError("Nomura listing has no paginationLabel count")
    text = normalize_title(label.get_text(" ", strip=True))
    match = COUNT_TEXT.search(text)
    if match is None:
        raise ValueError(f"Nomura listing count is not parseable: {text!r}")
    start, end, total = (int(value.replace(",", "")) for value in match.groups())
    if start < 1 or end < start or total < end:
        raise ValueError(f"Nomura listing count is inconsistent: {text!r}")
    return start, end, total


def _pagination_hrefs(soup: BeautifulSoup, response_url: str) -> dict[int, str]:
    """Map startrow -> exact pagination URL exposed by the page.

    The global/brand searches paginate with ``?startrow=N``; the regional
    categories append ``/<startrow>/`` after their category id.  The category
    id itself (e.g. 9050900) is never a startrow.
    """
    values: dict[int, str] = {}
    for anchor in soup.select("ul.pagination a[href]"):
        href = anchor.get("href") or ""
        match = re.search(r"[?&]startrow=(\d+)", href)
        if match is not None:
            startrow = int(match.group(1))
        else:
            path_match = re.search(r"/go/[^/]+/\d+/(\d+)/?$", urlsplit(urljoin(response_url, href)).path)
            if path_match is None:
                continue
            startrow = int(path_match.group(1))
        if startrow <= 0:
            continue
        values.setdefault(startrow, urljoin(response_url, href))
    return values


def _listing_row(tr) -> dict | None:
    anchor = tr.select_one("a.jobTitle-link")
    if anchor is None:
        return None
    href = anchor.get("href") or ""
    url = urljoin(CAREERS_ORIGIN, href)
    parsed = urlsplit(url)
    match = re.search(r"/(\d+)/?$", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "careers.nomura.com" or match is None:
        raise ValueError(f"Nomura listing row left the careers platform: {url!r}")
    brand = parsed.path.strip("/").split("/")[0]
    if brand not in BRANDS:
        raise ValueError(f"Nomura listing row has an unknown brand path: {url!r}")
    facility = tr.select_one(".colFacility .jobFacility") or tr.select_one(".jobFacility")
    location = tr.select_one(".colLocation .jobLocation") or tr.select_one(".jobLocation")
    title = normalize_title(anchor.get_text(" ", strip=True))
    if not title:
        raise ValueError(f"Nomura listing row has an empty title: {url!r}")
    return {
        "id": match.group(1),
        "title": title,
        "url": url,
        "brand": brand,
        "brand_label": BRANDS[brand],
        "division": normalize_title(facility.get_text(" ", strip=True)) if facility else None,
        "location": normalize_title(location.get_text(" ", strip=True)) if location else None,
    }


def _parse_rows(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for tr in soup.select("tr.data-row"):
        row = _listing_row(tr)
        if row is not None:
            rows.append(row)
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Nomura listing page repeats a job id")
    return rows


def _validate_search_path(response_url: str, *, brand: bool) -> None:
    parsed = urlsplit(response_url)
    expected = r"^/Nomura/search/?$" if brand else r"^/search/?$"
    if parsed.scheme != "https" or parsed.netloc != "careers.nomura.com" or re.fullmatch(expected, parsed.path) is None:
        raise ValueError(f"Nomura search response came from an unexpected route: {response_url!r}")


def parse_search_page(html: str, response_url: str = GLOBAL_SEARCH, *, brand: bool = False) -> dict:
    _validate_search_path(response_url, brand=brand)
    soup = BeautifulSoup(html, "lxml")
    rows = _parse_rows(soup)
    start, end, total = _pagination_counts(soup)
    if len(rows) != end - start + 1:
        raise ValueError("Nomura search row count does not match its own result range")
    return {
        "rows": rows,
        "start": start,
        "end": end,
        "total": total,
        "pagination": _pagination_hrefs(soup, response_url),
        "title": normalize_title(soup.title.get_text(" ", strip=True) if soup.title else ""),
    }


def parse_regional_page(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.nomura.com"
            or re.fullmatch(r"/Nomura/go/Career-Opportunities-[^/]+/\d+(?:/\d+)?/?", parsed.path) is None):
        raise ValueError(f"Nomura regional response came from an unexpected route: {response_url!r}")
    soup = BeautifulSoup(html, "lxml")
    rows = _parse_rows(soup)
    start, end, total = _pagination_counts(soup)
    if len(rows) != end - start + 1:
        raise ValueError("Nomura regional row count does not match its own result range")
    return {
        "rows": rows,
        "start": start,
        "end": end,
        "total": total,
        "pagination": _pagination_hrefs(soup, response_url),
        "title": normalize_title(soup.title.get_text(" ", strip=True) if soup.title else ""),
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.nomura.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Nomura official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "nomura" not in title.lower() or "early careers" not in title.lower():
        raise ValueError("Nomura official careers entry identity changed")
    headings = [normalize_title(h.get_text(" ", strip=True)) for h in soup.select("h1")]
    if "Nomura Careers" not in headings:
        raise ValueError("Nomura official careers entry heading is missing")
    links = {urljoin(response_url, a.get("href")) for a in soup.select("a[href]")}
    early = sorted(link for link in links if urlsplit(link).path.rstrip("/") == "/careers/early-careers")
    experienced = sorted(link for link in links if link == EXPERIENCED_ENTRY)
    if not early or not experienced:
        raise ValueError("Nomura official careers entry no longer publishes both journeys")
    return {
        "url": response_url,
        "title": title,
        "headings": headings,
        "early_careers_url": early[0],
        "experienced_url": experienced[0],
        "early_careers_links": len(early),
    }


def parse_early_careers(html: str, response_url: str = EARLY_CAREERS_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.nomura.com"
            or parsed.path.rstrip("/") != "/careers/early-careers"):
        raise ValueError("Nomura early careers page redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "nomura" not in title.lower() or "early careers" not in title.lower():
        raise ValueError("Nomura early careers page identity changed")
    tal_links = sorted({urljoin(response_url, a.get("href")) for a in soup.select("a[href]")
                        if TAL_HOST in (a.get("href") or "")})
    student = [link for link in tal_links if "/jobboard/vacancy/1/adv/" in link]
    events = [link for link in tal_links if "/jobboard/vacancy/2/adv/" in link]
    if not student or not events:
        raise ValueError("Nomura early careers page no longer links both TAL boards")
    return {
        "url": response_url,
        "title": title,
        "student_board_url": student[0],
        "events_board_url": events[0],
        "tal_links": tal_links,
    }


def parse_experienced_entry(html: str, response_url: str = EXPERIENCED_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.nomuraholdings.com"
            or parsed.path.rstrip("/") not in ("/en/careers", "/en/careers.html")):
        raise ValueError("Nomura experienced-professionals entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    links = sorted({urljoin(response_url, a.get("href")) for a in soup.select("a[href]")})
    regional = [link for link in links
                if re.fullmatch(r"https://www\.nomura\.com/(?:americas|asia|europe|india)/careers/?", link)]
    if len(regional) != 4:
        raise ValueError("Nomura experienced-professions page no longer links its four regional careers pages")
    return {"url": response_url, "regional_links": regional,
            "title": normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")}


def parse_tal_board(html: str, response_url: str = TAL_BOARD) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != TAL_HOST
            or "/jobboard/vacancy/1/adv/" not in parsed.path):
        raise ValueError("Nomura TAL student board redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    count_node = soup.select_one("div.results_meta h2")
    if count_node is None:
        raise ValueError("Nomura TAL student board has no result count")
    match = TAL_COUNT.search(normalize_title(count_node.get_text(" ", strip=True)))
    if match is None:
        raise ValueError("Nomura TAL student board result count is not parseable")
    count = int(match.group(1).replace(",", ""))
    rows = []
    for tr in soup.select("tr.details_row"):
        anchor = tr.select_one("a.subject")
        if anchor is None:
            continue
        opp_id = tr.get("data-oppid")
        if not opp_id or not str(opp_id).isdigit():
            raise ValueError("Nomura TAL row has no numeric opportunity id")
        url = urljoin(response_url, anchor.get("href"))
        if urlsplit(url).netloc != TAL_HOST or TAL_OPP.search(urlsplit(url).path) is None:
            raise ValueError(f"Nomura TAL row left the campus board: {url!r}")
        if TAL_OPP.search(urlsplit(url).path).group(1) != str(opp_id):
            raise ValueError("Nomura TAL row URL id does not match data-oppid")
        cells = tr.select("td")
        title = normalize_title(anchor.get_text(" ", strip=True))
        if not title or len(cells) < 3:
            raise ValueError(f"Nomura TAL row fields are incomplete: {url!r}")
        rows.append({
            "id": str(opp_id),
            "title": title,
            "url": url,
            "location": normalize_title(cells[1].get_text(" ", strip=True)),
            "application_deadline": normalize_title(cells[2].get_text(" ", strip=True)),
        })
    if len(rows) != count:
        raise ValueError(f"Nomura TAL board count {count} does not match {len(rows)} rows")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Nomura TAL board repeats an opportunity id")
    return {"rows": rows, "count": count,
            "title": normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")}


def parse_tal_events(html: str, response_url: str = TAL_EVENTS) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != TAL_HOST
            or "/jobboard/vacancy/2/adv/" not in parsed.path):
        raise ValueError("Nomura TAL events board redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    rows = [tr.get("data-oppid") for tr in soup.select("tr.details_row")]
    text = normalize_title(soup.get_text(" ", strip=True))
    return {"active_events": len(rows), "no_active_events": "no active events" in text.lower(),
            "title": normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")}


def _field_map(soup: BeautifulSoup) -> dict[str, object]:
    fields: dict[str, object] = {}
    for token in soup.select(".joblayouttoken"):
        label = token.select_one(".joblayouttoken-label")
        value = token.select_one("span[data-careersite-propertyid]")
        if label is not None and value is not None:
            fields[normalize_title(label.get_text(" ", strip=True)).rstrip(":")] = value
    return fields


def parse_radancy_detail(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    match = RADANCY_JOB.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "careers.nomura.com" or match is None:
        raise ValueError(f"Nomura detail response came from an unexpected route: {response_url!r}")
    brand, job_id = match.groups()
    soup = BeautifulSoup(html, "lxml")
    fields = _field_map(soup)
    required = ("Job Title", "Job Code", "Country", "City", "Skill Category", "Description")
    missing = [name for name in required if name not in fields]
    if missing:
        raise ValueError(f"Nomura detail page is missing fields: {missing}")
    description_node = fields["Description"]
    body = description_node.select_one(".jobdescription")
    if body is None:
        body = description_node
    apply_anchor = soup.select_one("a.apply")
    return {
        "job_id": job_id,
        "brand": brand,
        "brand_label": BRANDS[brand],
        "title": normalize_title(fields["Job Title"].get_text(" ", strip=True)),
        "job_code": normalize_title(fields["Job Code"].get_text(" ", strip=True)),
        "country": normalize_title(fields["Country"].get_text(" ", strip=True)),
        "city": normalize_title(fields["City"].get_text(" ", strip=True)),
        "skill_category": normalize_title(fields["Skill Category"].get_text(" ", strip=True)),
        "description_html": body.decode_contents(),
        "description_text": normalized_body_text(body.decode_contents()),
        "apply_url": urljoin(response_url, apply_anchor.get("href")) if apply_anchor else None,
        "raw_fields": {key: normalize_title(value.get_text(" ", strip=True)) for key, value in fields.items()},
    }


def parse_tal_detail(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    match = TAL_OPP.search(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != TAL_HOST or match is None:
        raise ValueError(f"Nomura TAL detail response came from an unexpected route: {response_url!r}")
    opp_id = match.group(1)
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, object] = {}
    for group in soup.select(".form-group"):
        label = group.select_one("label")
        value = group.select_one(".form-control-static") or group.select_one("[data-field_value]")
        if label is not None and value is not None:
            fields[normalize_title(label.get_text(" ", strip=True))] = value
    required = ("Region", "Division", "Location", "Program type", "Level", "Job description")
    missing = [name for name in required if name not in fields]
    if missing:
        raise ValueError(f"Nomura TAL detail page is missing fields: {missing}")
    heading = soup.select_one("h1")
    apply_form = soup.select_one("form.apply-form")
    description = fields["Job description"].decode_contents()
    return {
        "job_id": opp_id,
        "title": normalize_title(heading.get_text(" ", strip=True)) if heading else "",
        "region": normalize_title(fields["Region"].get_text(" ", strip=True)),
        "division": normalize_title(fields["Division"].get_text(" ", strip=True)),
        "location": normalize_title(fields["Location"].get_text(" ", strip=True)),
        "program_type": normalize_title(fields["Program type"].get_text(" ", strip=True)),
        "level": normalize_title(fields["Level"].get_text(" ", strip=True)),
        "description_html": description,
        "description_text": normalized_body_text(description),
        "apply_url": urljoin(response_url, apply_form.get("action")) if apply_form else None,
        "raw_fields": {key: normalize_title(value.get_text(" ", strip=True)) for key, value in fields.items()},
    }


def parse_sitemap(xml: str) -> dict:
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>([^<]+)</loc>", block)
        if loc is None:
            continue
        lastmod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        entries.append((loc.group(1), lastmod.group(1) if lastmod else None))
    jobs = {}
    for url, lastmod in entries:
        parsed = urlsplit(url)
        match = RADANCY_JOB.fullmatch(parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "careers.nomura.com" or match is None:
            raise ValueError(f"Nomura sitemap contains an unknown job URL: {url!r}")
        job_id = match.group(2)
        if job_id in jobs:
            raise ValueError(f"Nomura sitemap repeats job id {job_id}")
        jobs[job_id] = {"url": url, "lastmod": lastmod or None}
    return jobs


def _deadline_iso(value: str | None) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", value.strip())
    if match is None:
        return None
    months = {name: index for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
    month = months.get(match.group(2).title())
    if month is None:
        return None
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"


class Nomura(Adapter):
    """Follow Nomura's published global careers search and TAL campus board."""

    VERIFICATION_REVISION = "nomura.careers-global-search-and-tal-campus.1"

    async def _fetch(self, url: str, label: str):
        response = await self.http.get(url, headers=BROWSER_HEADERS)
        if response.status_code != 200:
            raise ValueError(f"Nomura {label} returned HTTP {response.status_code}: {url}")
        return response

    async def _search_pages(self, first_url: str, *, brand: bool, label: str) -> dict:
        first = await self._fetch(first_url, label)
        page = parse_search_page(first.text, str(first.url), brand=brand)
        total = page["total"]
        rows = list(page["rows"])
        pages = 1
        seen_starts = {page["start"]}
        for startrow, href in sorted(page["pagination"].items()):
            if len(rows) >= total:
                break
            if startrow in seen_starts:
                continue
            if not self.page_guard():
                raise ValueError(f"Nomura {label} page budget exhausted")
            response = await self._fetch(href, label)
            next_page = parse_search_page(response.text, str(response.url), brand=brand)
            if next_page["total"] != total:
                raise ValueError(f"Nomura {label} total changed between pages")
            if next_page["start"] != startrow + 1:
                raise ValueError(f"Nomura {label} pagination startrow does not match its rows")
            rows.extend(next_page["rows"])
            seen_starts.add(next_page["start"])
            pages += 1
        if len(rows) != total:
            raise ValueError(f"Nomura {label} collected {len(rows)} rows but the site reports {total}")
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError(f"Nomura {label} repeats a job id across pages")
        return {"rows": rows, "total": total, "pages": pages,
                "ids": [row["id"] for row in rows], "title": page["title"]}

    async def _regional_pages(self, first_url: str, label: str) -> dict:
        first = await self._fetch(first_url, label)
        page = parse_regional_page(first.text, str(first.url))
        total = page["total"]
        rows = list(page["rows"])
        pages = 1
        seen_starts = {page["start"]}
        for startrow, href in sorted(page["pagination"].items()):
            if len(rows) >= total:
                break
            if startrow in seen_starts:
                continue
            if not self.page_guard():
                raise ValueError(f"Nomura {label} page budget exhausted")
            response = await self._fetch(href, label)
            next_page = parse_regional_page(response.text, str(response.url))
            if next_page["total"] != total:
                raise ValueError(f"Nomura {label} total changed between pages")
            rows.extend(next_page["rows"])
            seen_starts.add(next_page["start"])
            pages += 1
        if len(rows) != total:
            raise ValueError(f"Nomura {label} collected {len(rows)} rows but the site reports {total}")
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError(f"Nomura {label} repeats a job id across pages")
        return {"rows": rows, "total": total, "pages": pages, "ids": [row["id"] for row in rows]}

    async def run(self):
        started = time.perf_counter()
        metadata = self.result.metadata
        metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": GLOBAL_SEARCH,
            "listing_authority": (
                "Nomura's own careers platform: the unfiltered site-wide search at "
                "careers.nomura.com/search/ (494 postings), reconciled with the four "
                "regional Career Opportunities categories (492) and the Nomura-brand "
                "search (486), plus the TAL Student Opportunities board (23) linked "
                "from Nomura's official Early Careers page."
            ),
            "detail_method": (
                "Radancy/SuccessFactors server-rendered .joblayouttoken detail pages for "
                "experienced roles; TAL .form-group detail pages for campus opportunities."
            ),
            "date_semantics": (
                "No posted/updated date is rendered on the Radancy listing or detail pages; "
                "date_posted/date_updated stay null. TAL application deadlines are real "
                "deadlines and are retained as valid_through plus raw.application_deadline. "
                "The sitemap lastmod (all 2026-10-31) is a build date and is never used as a "
                "posting date."
            ),
            "membership_sources": {
                "global_search": GLOBAL_SEARCH,
                "nomura_brand_search": BRAND_SEARCH,
                "tal_student_board": TAL_BOARD,
                "tal_events_board": TAL_EVENTS,
                "sitemap": SITEMAP,
                "regional_categories": REGIONAL_CATEGORIES,
            },
        })

        # 1. Official entry -> early careers + experienced professionals.
        official = await self._fetch(OFFICIAL_ENTRY, "official entry")
        official_parsed = parse_official_entry(official.text, str(official.url))
        early = await self._fetch(official_parsed["early_careers_url"], "early careers entry")
        early_parsed = parse_early_careers(early.text, str(early.url))
        experienced = await self._fetch(EXPERIENCED_ENTRY, "experienced-professionals entry")
        experienced_parsed = parse_experienced_entry(experienced.text, str(experienced.url))
        metadata["official_entry"] = official_parsed
        metadata["early_careers_entry"] = early_parsed
        metadata["experienced_entry"] = experienced_parsed

        # 2. TAL campus board + events board (events are recorded, not collected).
        tal_response = await self._fetch(early_parsed["student_board_url"], "TAL student board")
        tal = parse_tal_board(tal_response.text, str(tal_response.url))
        events_response = await self._fetch(early_parsed["events_board_url"], "TAL events board")
        events = parse_tal_events(events_response.text, str(events_response.url))
        metadata["tal_student_board"] = {
            "url": str(tal_response.url), "count": tal["count"], "title": tal["title"],
            "ids": [row["id"] for row in tal["rows"]],
            "row_fields": ["data-oppid", "data-title", "title", "location", "application_deadline", "url"],
        }
        metadata["tal_events_board"] = {"url": str(events_response.url), **events}

        # 3. Site-wide search and Nomura-brand search.
        listing_started = time.perf_counter()
        global_search = await self._search_pages(GLOBAL_SEARCH, brand=False, label="global search")
        brand_search = await self._search_pages(BRAND_SEARCH, brand=True, label="Nomura-brand search")
        metadata["global_search"] = {
            "url": GLOBAL_SEARCH, "total": global_search["total"], "pages": global_search["pages"],
            "ids": global_search["ids"],
            "brand_counts": {brand: sum(1 for row in global_search["rows"] if row["brand"] == brand)
                             for brand in BRANDS},
            "page_size": len(global_search["rows"]) // global_search["pages"] if global_search["pages"] else None,
        }
        metadata["nomura_brand_search"] = {
            "url": BRAND_SEARCH, "total": brand_search["total"], "pages": brand_search["pages"],
            "ids": brand_search["ids"],
        }

        # 4. Four regional Career Opportunities categories (reconciliation).
        regional = {}
        for name, url in REGIONAL_CATEGORIES.items():
            regional[name] = await self._regional_pages(url, f"{name} regional listing")
        metadata["regional_listings"] = {
            name: {"url": REGIONAL_CATEGORIES[name], "total": value["total"],
                   "pages": value["pages"], "ids": value["ids"]}
            for name, value in regional.items()
        }
        regional_ids = {row["id"] for value in regional.values() for row in value["rows"]}
        global_ids = {row["id"] for row in global_search["rows"]}
        brand_ids = {row["id"] for row in brand_search["rows"]}
        sitemap_response = await self._fetch(SITEMAP, "sitemap")
        sitemap = parse_sitemap(sitemap_response.text)
        metadata["sitemap"] = {
            "url": SITEMAP, "job_urls": len(sitemap),
            "matches_global_search": set(sitemap) == global_ids,
            "lastmod_values": sorted({value["lastmod"] for value in sitemap.values() if value["lastmod"]}),
        }
        missing_from_regional = sorted(global_ids - regional_ids)
        missing_from_brand = sorted(global_ids - brand_ids)
        metadata["membership_reconciliation"] = {
            "global_total": len(global_ids),
            "regional_total": len(regional_ids),
            "brand_total": len(brand_ids),
            "regional_is_subset_of_global": regional_ids <= global_ids,
            "brand_is_subset_of_global": brand_ids <= global_ids,
            "global_only_ids": missing_from_regional,
            "global_only_rows": [row for row in global_search["rows"] if row["id"] in set(missing_from_regional)],
            "sibling_brand_ids": sorted(global_ids - brand_ids),
            "sibling_brand_rows": [row for row in global_search["rows"] if row["id"] in set(missing_from_brand)],
            "global_brand_counts": {
                brand: sum(1 for row in global_search["rows"] if row["brand"] == brand) for brand in BRANDS
            },
            "regional_rows_outside_global": sorted(regional_ids - global_ids),
            "brand_rows_outside_global": sorted(brand_ids - global_ids),
        }
        metadata["listing_phase_seconds"] = round(max(0.0, time.perf_counter() - listing_started), 6)

        # 5. Build the complete membership and hydrate every exact detail page.
        checkpoint = self.options.checkpoint
        self.options.checkpoint = None
        try:
            for row in global_search["rows"]:
                raw = {
                    "membership_source": "careers_site_global_search",
                    "brand": row["brand"],
                    "brand_label": row["brand_label"],
                    "posting_kind": ("talent_community"
                                     if "talent community" in row["title"].lower() else "job"),
                    "listing": row,
                    "sitemap_lastmod": sitemap.get(row["id"], {}).get("lastmod"),
                }
                job = self.job(
                    raw, title=row["title"], url=row["url"], job_id=row["id"],
                    locations=[{"raw": row["location"], "source": "listing"}],
                    department=None, employer=row["brand_label"], language="en",
                )
                self.add(job)
            for row in tal["rows"]:
                raw = {
                    "membership_source": "tal_campus",
                    "brand": "Nomura",
                    "brand_label": "Nomura",
                    "posting_kind": "student_opportunity",
                    "listing": row,
                    "application_deadline": row["application_deadline"],
                }
                job = self.job(
                    raw, title=row["title"], url=row["url"], job_id=row["id"],
                    locations=[{"raw": row["location"], "source": "listing"}],
                    employer="Nomura", language="en",
                    valid_through=_deadline_iso(row["application_deadline"]),
                )
                self.add(job)
        finally:
            self.options.checkpoint = checkpoint

        self.result.expected_total = len(global_search["rows"]) + len(tal["rows"])
        if len(self.result.jobs) != self.result.expected_total:
            self.result.errors.append(
                f"membership_count_mismatch: {len(self.result.jobs)} collected vs "
                f"{self.result.expected_total} listed")

        detail_started = time.perf_counter()
        jobs = await self.hydrate(self.result.jobs, self.detail)
        metadata["detail_phase_wall_seconds"] = round(max(0.0, time.perf_counter() - detail_started), 6)

        # Listing identity is authoritative even when a resumed run reuses a
        # verified cached detail record (the cache carries the old raw payload).
        global_by_id = {row["id"]: row for row in global_search["rows"]}
        for job in jobs:
            if job.raw.get("membership_source") == "tal_campus":
                job.raw.update({"brand": "Nomura", "brand_label": "Nomura",
                                "posting_kind": "student_opportunity"})
                continue
            row = global_by_id.get(job.job_id)
            if row is not None:
                job.raw.update({
                    "brand": row["brand"],
                    "brand_label": row["brand_label"],
                    "posting_kind": ("talent_community"
                                     if "talent community" in row["title"].lower() else "job"),
                })

        failed = [job for job in jobs if not job.description_verified]
        failed_fetch = [job for job in jobs if "detail_fetch_failed" in job.warnings]
        publisher_empty = [job for job in jobs if job.description_unavailable]
        metadata["membership"] = {
            "global_search_total": global_search["total"],
            "tal_student_total": tal["count"],
            "expected_total": self.result.expected_total,
            "collected": len(jobs),
            "by_source": {
                "careers_site_global_search": sum(1 for job in jobs if job.raw.get("membership_source") == "careers_site_global_search"),
                "tal_campus": sum(1 for job in jobs if job.raw.get("membership_source") == "tal_campus"),
            },
            "by_brand": {brand: sum(1 for job in jobs if job.raw.get("brand") == brand) for brand in BRANDS},
            "failed_ids": [job.job_id for job in failed_fetch],
            "failed_urls": [job.url for job in failed_fetch],
            "unverified_ids": [job.job_id for job in failed],
            "publisher_empty_ids": [job.job_id for job in publisher_empty],
            "publisher_empty_count": len(publisher_empty),
            "publisher_empty_examples": {
                job.job_id: job.raw.get("publisher_description_text") for job in publisher_empty
            },
            "talent_pipeline_ids": [job.job_id for job in jobs if "talent community" in job.title.lower()],
        }
        self.result.listing_complete = (
            not self.result.errors
            and len(jobs) == self.result.expected_total
            and set(sitemap) == global_ids
            and regional_ids <= global_ids
            and brand_ids <= global_ids
        )
        self.result.timings["total_run_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        self.result.finish()
        return self.result

    async def detail(self, job):
        if urlsplit(job.url).netloc == TAL_HOST:
            return await self.tal_detail(job)
        return await self.radancy_detail(job)

    async def radancy_detail(self, job):
        response = await self._fetch(job.url, "job detail")
        parsed = parse_radancy_detail(response.text, str(response.url))
        if parsed["job_id"] != job.job_id:
            raise ValueError("Nomura detail URL id does not match the listed id")
        if parsed["brand"] != job.raw.get("brand"):
            raise ValueError("Nomura detail brand path does not match the listed brand")
        if normalize_title(parsed["title"]) != normalize_title(job.title):
            raise ValueError(
                f"Nomura detail title changed: {job.title!r} -> {parsed['title']!r}")
        job.title = parsed["title"]
        job.requisition_id = parsed["job_code"] or None
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "radancy_joblayouttoken_description"
        job.description_source_url = str(response.url)
        short = 0 < len(parsed["description_text"].strip()) < 100
        job.description_unavailable = short or not bool(parsed["description_text"].strip())
        if short:
            job.raw["publisher_description_text"] = parsed["description_text"]
            job.warnings.append("publisher_empty_description")
        job.department = parsed["skill_category"] or None
        job.employer = parsed["brand_label"]
        job.locations = [{
            "raw": job.raw["listing"].get("location"),
            "city": parsed["city"] or None,
            "country": parsed["country"] or None,
            "source": "listing+detail",
        }]
        job.raw.update({
            "detail_url": str(response.url),
            "detail_fields": parsed["raw_fields"],
            "apply_url": parsed["apply_url"],
            "brand": parsed["brand"],
            "brand_label": parsed["brand_label"],
        })
        return job.validate()

    async def tal_detail(self, job):
        response = await self._fetch(job.url, "TAL detail")
        parsed = parse_tal_detail(response.text, str(response.url))
        if parsed["job_id"] != job.job_id:
            raise ValueError("Nomura TAL detail URL id does not match the listed id")
        if normalize_title(parsed["title"]) != normalize_title(job.title):
            raise ValueError(
                f"Nomura TAL detail title changed: {job.title!r} -> {parsed['title']!r}")
        job.title = parsed["title"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "tal_form_group_job_description"
        job.description_source_url = str(response.url)
        short = 0 < len(parsed["description_text"].strip()) < 100
        job.description_unavailable = short or not bool(parsed["description_text"].strip())
        if short:
            job.raw["publisher_description_text"] = parsed["description_text"]
            job.warnings.append("publisher_empty_description")
        job.department = parsed["division"] or None
        job.employment_type = parsed["program_type"] or None
        job.experience = parsed["level"] or None
        job.employer = "Nomura"
        job.locations = [{
            "raw": job.raw["listing"].get("location"),
            "city": parsed["location"] or None,
            "region": parsed["region"] or None,
            "source": "listing+detail",
        }]
        job.valid_through = _deadline_iso(job.raw.get("application_deadline"))
        job.raw.update({
            "detail_url": str(response.url),
            "detail_fields": parsed["raw_fields"],
            "apply_url": parsed["apply_url"],
            "application_deadline": job.raw.get("application_deadline"),
            "program_type": parsed["program_type"],
            "level": parsed["level"],
        })
        return job.validate()
