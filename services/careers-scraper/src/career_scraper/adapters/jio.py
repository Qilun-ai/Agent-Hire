"""Jio's own careers portal: the server-rendered ASP.NET WebForms job journey.

The official entry ``https://careers.jio.com/`` publishes **Job Opportunities**
at ``frmJobCategories.aspx``.  That page enumerates the complete public
function taxonomy (24 rows, including empty ones) and links each function to
its ``frmfuncwisejob.aspx`` posting list.  Each list is an ASP.NET DataPager
(10 by default, 5/10/15/20/25 selectable) over the same public rows, and each
row links to exactly one ``frmjobdescription.aspx`` page.  The detail page
carries the seven visible fields: Posted Date, Function/Business Area,
Location, Job Responsibilities, Education Requirement, Experience Requirement
and Skills & Competencies.

Membership is therefore the union of every function list the official
categories page publishes, unfiltered.  No ATS, parent-company board or
aggregator is used.  The site's only language option is English.  Apply is a
login-gated postback on the exact detail page, so that page URL is the apply
entry point; no form is filled or submitted.
"""

from __future__ import annotations

from datetime import datetime
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.jio.com/"
CATEGORIES_URL = "https://careers.jio.com/frmJobCategories.aspx"
LISTING_PATH = "/frmfuncwisejob.aspx"
DETAIL_PATH = "/frmjobdescription.aspx"
HOST = "careers.jio.com"
CATEGORY_QUERY_KEYS = {"func", "desc", "flag"}
DETAIL_QUERY_KEYS = {"JBTITLE", "jbID", "funcCode"}
JOB_ID_SUFFIX = re.compile(r"\(\s*(\d+)\s*\)\s*$")
PAGER_LABEL = re.compile(r"Showing\s+(\d+)\s+of\s+(\d+)\s+Pages", re.I)
POSTED_FORMATS = ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d/%m/%Y")
DETAIL_SECTIONS = (
    ("responsibilities", "MainContent_lblSummRolelbl", "MainContent_lblSummRole", "Job Responsibilities"),
    ("education", "MainContent_lblEduReqlbl", "MainContent_lblEduReq", "Education Requirement"),
    ("experience", "MainContent_lblExpReqlbl", "MainContent_lblExpReq", "Experience Requirement"),
    ("skills", "MainContent_lblSkilllbl", "MainContent_lblSkill", "Skills & Competencies"),
)
MAX_ITEMS_PER_PAGE = 25
SPECIALIZED_LISTS = (
    ("PWD Opportunity at Jio", "/frmfttxjobs.aspx"),
    ("Jio Point Jobs", "/frmPointjob.aspx"),
    ("Jio Certified Freelancer Technician", "/frmSecondCareer.aspx"),
)
SPECIALIZED_PATHS = {path for _, path in SPECIALIZED_LISTS}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def normalized_body_text(html: str | None) -> str:
    return normalized(text_html(html or ""))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_posted_date(value: str | None) -> tuple[str | None, str | None]:
    """Return ISO date and a diagnostic; never substitute a request date."""
    raw = normalized(value)
    if not raw:
        return None, "empty_posted_date"
    for fmt in POSTED_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d"), None
        except ValueError:
            continue
    return None, "unparseable_posted_date"


def raw_query_dict(query: str) -> dict:
    """Decode an ASP.NET query string without turning literal '+' into space.

    Jio's link tokens are base64 values containing '+' and '//' characters.
    The browser URL keeps them literal; provenance must not rewrite them.
    """
    values = {}
    for pair in (query or "").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        values[unquote(key)] = unquote(value)
    return values


def _check_origin(url: str, path: str | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise ValueError(f"Jio response came from an unexpected origin: {url}")
    if path is not None and parts.path != path:
        raise ValueError(f"Jio response came from an unexpected path: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _check_origin(response_url, "/")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Reliance Jio Careers" not in title:
        raise ValueError("Jio official careers entry title changed")
    organizations = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            import json

            value = json.loads(node.get_text())
        except ValueError:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Organization":
                organizations.append(item)
    names = [normalized(item.get("name")) for item in organizations]
    if "Reliance Jio Infocomm Limited" not in names:
        raise ValueError("Jio official careers entry no longer declares its employer organization")
    if "frmJobCategories.aspx" not in html:
        raise ValueError("Jio official careers entry no longer publishes the Job Opportunities route")
    return {
        "url": response_url,
        "title": title,
        "organization": "Reliance Jio Infocomm Limited",
        "job_opportunities_route": "frmJobCategories.aspx",
    }


def parse_categories(html: str, response_url: str = CATEGORIES_URL) -> dict:
    _check_origin(response_url, "/frmJobCategories.aspx")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "jio careers" not in page_title.lower():
        raise ValueError("Jio categories page title changed")
    if not any(
        normalized(node.get_text(" ", strip=True)).lower() == "our featured categories"
        for node in soup.select("span[role=heading],h1,h2,h3")
    ):
        raise ValueError("Jio categories page no longer publishes its featured-category heading")
    rows, issues = [], []
    for li in soup.select("li.list-cont"):
        anchor = li.select_one('a[href^="frmfuncwisejob" i]')
        code = li.select_one('input[id*="hdfunctioncode"]')
        description = li.select_one('input[id*="hdDescription"]')
        count = li.select_one('span[id*="lblfunctionjobCount"]')
        label = li.select_one('span[id*="lblfunctional"]')
        if not (anchor and code and description and count):
            issues.append("categories_row_missing_link_or_count")
            continue
        name = normalized(description.get("value") or (label.get_text(" ", strip=True) if label else ""))
        code_value = (code.get("value") or "").strip()
        try:
            count_value = int(normalized(count.get_text(" ", strip=True)).replace(",", ""))
        except ValueError:
            issues.append(f"categories_row_bad_count:{name}")
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        parts = urlsplit(target)
        query = raw_query_dict(parts.query)
        if (parts.scheme != "https" or parts.hostname != HOST or parts.path != LISTING_PATH
                or set(query) != CATEGORY_QUERY_KEYS
                or any(not normalized(values) for values in query.values())):
            issues.append(f"categories_row_unexpected_listing_route:{name}")
            continue
        rows.append({
            "code": code_value,
            "name": name,
            "count": count_value,
            "url": target,
            "query": query,
            "query_raw": parts.query,
        })
    if len(rows) != 24:
        issues.append(f"categories_taxonomy_size_changed:{len(rows)}")
    if len({row["code"] for row in rows}) != len(rows):
        issues.append("categories_duplicate_function_code")
    if len({row["name"] for row in rows}) != len(rows):
        issues.append("categories_duplicate_function_name")
    advertised_total = sum(row["count"] for row in rows)
    if advertised_total <= 0:
        issues.append("categories_has_no_public_jobs")
    return {
        "url": response_url,
        "page_title": page_title,
        "categories": rows,
        "advertised_total": advertised_total,
        "nonzero_categories": sum(1 for row in rows if row["count"] > 0),
        "issues": issues,
    }


def parse_listing_page(html: str, response_url: str, category_name: str | None = None) -> dict:
    """Parse an exact function list page, including its ASP.NET pager state."""
    _check_origin(response_url, LISTING_PATH)
    soup = BeautifulSoup(html, "lxml")
    pager = None
    for node in soup.select("span[id]"):
        if node.get("id", "").endswith("CurrentPageLabel"):
            pager = PAGER_LABEL.search(normalized(node.get_text(" ", strip=True)))
            break
    if pager is None:
        raise ValueError("Jio listing page no longer publishes its DataPager label")
    current_page, total_pages = int(pager.group(1)), int(pager.group(2))
    if current_page < 1 or total_pages < 1 or current_page > total_pages:
        raise ValueError("Jio listing page DataPager state is invalid")

    rows, issues, seen = [], [], set()
    for anchor in soup.select('a[id^="MainContent_lstJoblist_hylUser_"]'):
        title = normalized(anchor.get_text(" ", strip=True))
        match = JOB_ID_SUFFIX.search(title)
        if match is None:
            issues.append(f"listing_row_title_without_job_id:{title[:80]}")
            continue
        job_id = match.group(1)
        if job_id in seen:
            issues.append(f"listing_duplicate_job_id_on_page:{job_id}")
            continue
        href = anchor.get("href") or ""
        detail_url = urljoin(response_url, href)
        parts = urlsplit(detail_url)
        query = raw_query_dict(parts.query)
        if (parts.scheme != "https" or parts.hostname != HOST or parts.path != DETAIL_PATH
                or set(query) != DETAIL_QUERY_KEYS
                or any(not normalized(values) for values in query.values())):
            issues.append(f"listing_row_unexpected_detail_route:{job_id}")
            continue
        # Every row is a sibling inside one large container; the <p> that
        # belongs to this row is the next sibling of its outer `aJobDesc`, not
        # the first <p> in the container.
        row_anchor = anchor.find_parent("a", id="aJobDesc") or anchor.parent
        paragraph = row_anchor.find_next_sibling("p") if row_anchor else None
        spans = paragraph.find_all("span", recursive=False) if paragraph else []
        if len(spans) < 3:
            issues.append(f"listing_row_missing_location_function_or_date:{job_id}")
            continue
        location = normalized(spans[0].get_text(" ", strip=True))
        function = normalized(spans[1].get_text(" ", strip=True))
        posted_raw = normalized(spans[2].get_text(" ", strip=True))
        if category_name is not None and function != normalized(category_name):
            issues.append(f"listing_row_function_mismatch:{job_id}:{function}")
        rows.append({
            "line": len(rows),
            "job_id": job_id,
            "title": title,
            "location": location,
            "function": function,
            "posted_date_raw": posted_raw,
            "detail_url": detail_url,
            "detail_query": query,
            "detail_query_raw": parts.query,
            "source_kind": "function_list",
        })
        seen.add(job_id)
    return {
        "url": response_url,
        "current_page": current_page,
        "total_pages": total_pages,
        "rows": rows,
        "entries_on_page": len(rows),
        "issues": issues,
    }


def parse_detail_page(html: str, response_url: str, listing_row: dict | None = None) -> dict:
    """Read the seven visible detail fields and the exact listing identity."""
    _check_origin(response_url, DETAIL_PATH)
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.find(id="MainContent_lblJobTitle")
    if title_node is None:
        raise ValueError("Jio job detail page no longer publishes its title")
    title = normalized(title_node.get_text(" ", strip=True))
    match = JOB_ID_SUFFIX.search(title)
    if match is None:
        raise ValueError(f"Jio job detail title has no posting id: {title[:100]}")
    job_id = match.group(1)
    if listing_row is not None:
        if str(listing_row.get("job_id")) != job_id:
            raise ValueError(
                f"Jio detail page posting id {job_id} does not match listing id {listing_row.get('job_id')}"
            )
        if normalized(listing_row.get("title")) != title:
            raise ValueError("Jio detail page title does not match its exact listing row")

    labels, issues = {}, []
    for key, label_id, value_id, expected_label in DETAIL_SECTIONS:
        label_node = soup.find(id=label_id)
        value_node = soup.find(id=value_id)
        if label_node is None or value_node is None:
            issues.append(f"detail_missing_section:{key}")
            continue
        label = normalized(label_node.get_text(" ", strip=True))
        if label != expected_label:
            issues.append(f"detail_section_label_changed:{key}:{label}")
        labels[key] = label
        value_html = "".join(str(child) for child in value_node.contents).strip()
        value_text = text_html(value_html)
        labels[f"{key}_html"] = value_html
        labels[f"{key}_text"] = value_text

    sections = []
    for key, _, _, expected_label in DETAIL_SECTIONS:
        section_html = labels.get(f"{key}_html") or ""
        section_text = normalized(labels.get(f"{key}_text") or "")
        if section_text:
            sections.append({
                "key": key,
                "heading": labels.get(key, expected_label),
                "html": section_html,
                "text": section_text,
            })
    if sections:
        description_html = "\n".join(
            f'<h2>{section["heading"]}</h2>\n<div>{section["html"]}</div>' for section in sections
        )
    else:
        description_html = ""
    description_text = text_html(description_html)
    description_normalized = normalized_body_text(description_html)

    posted_node = soup.find(id="MainContent_lblPostedDate")
    posted_raw = normalized(posted_node.get_text(" ", strip=True)) if posted_node else ""
    date_posted, date_issue = parse_posted_date(posted_raw)
    function_node = soup.find(id="MainContent_lblSec")
    location_node = soup.find(id="MainContent_lblLoc")
    function = normalized(function_node.get_text(" ", strip=True)) if function_node else ""
    location = normalized(location_node.get_text(" ", strip=True)) if location_node else ""
    if not function:
        issues.append("detail_missing_function")
    if not location:
        issues.append("detail_missing_location")
    if listing_row is not None:
        if function and function != normalized(listing_row.get("function")):
            issues.append(f"detail_function_mismatch:{normalized(listing_row.get('function'))}->{function}")
        if location and location != normalized(listing_row.get("location")):
            issues.append(f"detail_location_mismatch:{normalized(listing_row.get('location'))}->{location}")
        if posted_raw and posted_raw != normalized(listing_row.get("posted_date_raw")):
            issues.append(f"detail_posted_date_mismatch:{normalized(listing_row.get('posted_date_raw'))}->{posted_raw}")

    lat = soup.find(id="MainContent_hdnLatidute")
    lon = soup.find(id="MainContent_hdnLagtidue")
    languages = []
    select = soup.find(id="MainContent_ddlLanguageSelection")
    if select is not None:
        for option in select.find_all("option"):
            languages.append({
                "value": option.get("value"),
                "label": normalized(option.get_text(" ", strip=True)),
                "selected": option.has_attr("selected"),
            })
    apply_controls = [
        {"name": node.get("name"), "value": node.get("value"), "id": node.get("id")}
        for node in soup.select('input[type="submit"][name*="pply"]')
    ]
    query = raw_query_dict(urlsplit(response_url).query)
    return {
        "url": response_url,
        "job_id": job_id,
        "title": title,
        "posted_date_raw": posted_raw,
        "date_posted": date_posted,
        "date_issue": date_issue,
        "function": function,
        "location": location,
        "labels": labels,
        "sections": sections,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized": description_normalized,
        "description_normalized_chars": len(description_normalized),
        "description_fnv1a64": fnv1a64(description_normalized),
        "description_unavailable": not sections,
        "apply_controls": apply_controls,
        "language_options": languages,
        "coordinates": {
            "latitude_raw": lat.get("value") if lat else None,
            "longitude_raw": lon.get("value") if lon else None,
        },
        "query": query,
        "query_raw": urlsplit(response_url).query,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_specialized_page(html: str, response_url: str, list_name: str,
                           known_functions: set[str] | None = None) -> dict:
    """Parse one row-only official spotlight list (no per-job detail page).

    PWD, Jio Point and Jio Certified Freelancer Technician are linked from the
    official home page as "See jobs".  Their rows publish title/id, location,
    function, posted date and a "Submit Interest" control, but no
    ``frmjobdescription.aspx`` link.  They are retained as listing-only public
    postings; the page's generic marketing copy is never treated as a job body.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != HOST
            or parts.path not in SPECIALIZED_PATHS):
        raise ValueError(f"Jio specialized list came from an unexpected route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    pager = None
    for node in soup.select("span[id]"):
        if node.get("id", "").endswith("CurrentPageLabel"):
            pager = PAGER_LABEL.search(normalized(node.get_text(" ", strip=True)))
            break
    if pager is None:
        raise ValueError(f"Jio specialized list {list_name} no longer publishes its DataPager label")
    current_page, total_pages = int(pager.group(1)), int(pager.group(2))
    if current_page < 1 or total_pages < 1 or current_page > total_pages:
        raise ValueError(f"Jio specialized list {list_name} DataPager state is invalid")
    rows, issues, seen = [], [], set()
    for figure in soup.select("figure.jobPointList"):
        title_node = figure.select_one('span[id*="hylUser"]')
        if title_node is None:
            issues.append("specialized_row_title_missing")
            continue
        title = normalized(title_node.get_text(" ", strip=True))
        match = JOB_ID_SUFFIX.search(title)
        if match is None:
            issues.append(f"specialized_row_title_without_job_id:{title[:80]}")
            continue
        job_id = match.group(1)
        if job_id in seen:
            issues.append(f"specialized_duplicate_job_id_on_page:{job_id}")
            continue
        spans = figure.select("p span")
        if len(spans) < 3:
            issues.append(f"specialized_row_missing_location_function_or_date:{job_id}")
            continue
        location = normalized(spans[0].get_text(" ", strip=True))
        function = normalized(spans[1].get_text(" ", strip=True))
        posted_raw = normalized(spans[2].get_text(" ", strip=True))
        if known_functions is not None and function not in known_functions:
            issues.append(f"specialized_row_unknown_function:{job_id}:{function}")
        button = figure.select_one('input[id*="btns"]')
        rows.append({
            "line": len(rows),
            "job_id": job_id,
            "title": title,
            "location": location,
            "function": function,
            "posted_date_raw": posted_raw,
            "detail_url": None,
            "listing_url": response_url,
            "apply_control_name": button.get("name") if button else None,
            "apply_control_value": button.get("value") if button else None,
            "list_name": list_name,
            "source_kind": "specialized_listing",
        })
        seen.add(job_id)
    return {
        "url": response_url,
        "list_name": list_name,
        "current_page": current_page,
        "total_pages": total_pages,
        "rows": rows,
        "entries_on_page": len(rows),
        "issues": issues,
    }


def form_state(html: str) -> dict:
    """Recreate the browser's ASP.NET WebForms POST state for pager actions."""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    if form is None:
        raise ValueError("Jio page no longer contains a form")
    data: dict[str, str] = {}
    for node in form.select("input"):
        name = node.get("name")
        if not name:
            continue
        kind = (node.get("type") or "text").lower()
        if kind in {"submit", "button", "image", "file"}:
            continue
        if kind in {"checkbox", "radio"} and not node.has_attr("checked"):
            continue
        if kind == "reset":
            continue
        data[name] = node.get("value") or ""
    for node in form.select("select"):
        name = node.get("name")
        if not name:
            continue
        selected = node.select_one("option[selected]") or node.find("option")
        data[name] = selected.get("value") if selected and selected.get("value") is not None else ""
    for node in form.select("textarea"):
        name = node.get("name")
        if name:
            data[name] = node.get_text()
    return data


ENTRIES_CONTROL = "ctl00$MainContent$ddlentries"
NEXT_CONTROL = "ctl00$MainContent$lstJoblist$DataPager1$ctl00$lnkNext"


class Jio(Adapter):
    """Enumerate every function list published by Jio's own careers portal."""

    VERIFICATION_REVISION = "jio.careers.webforms-categories.1"

    def __init__(self, http, company, source, options, *, category_filter=None,
                 include_specialized=False):
        super().__init__(http, company, source, options)
        self.category_filter = set(category_filter) if category_filter else None
        self.include_specialized = include_specialized

    async def _fetch_categories(self):
        started = time.perf_counter()
        response = await self.http.get(CATEGORIES_URL, kind="categories", extra={"phase": "categories"})
        parsed = parse_categories(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
        })
        return parsed

    async def _fetch_category(self, category):
        """Walk one function's official DataPager list from page 1 to last."""
        started = time.perf_counter()
        url = category["url"]
        report = {
            "code": category["code"],
            "name": category["name"],
            "advertised_count": category["count"],
            "url": url,
            "pages": [],
            "issues": [],
        }
        response = await self.http.get(url, kind="listing", extra={"category": category["name"], "mode": "page1"})
        page = parse_listing_page(response.text, str(response.url), category["name"])
        report["initial_entries"] = page["entries_on_page"]
        report["issues"].extend(page["issues"])
        initial_page_rows = list(page["rows"])
        report["default_view_rows"] = len(initial_page_rows)
        state = form_state(response.text)
        state[ENTRIES_CONTROL] = str(MAX_ITEMS_PER_PAGE)
        state["__EVENTTARGET"] = ENTRIES_CONTROL
        state["__EVENTARGUMENT"] = ""
        response = await self.http.post(url, data=state, kind="listing",
                                        extra={"category": category["name"], "mode": "entries25"})
        page = parse_listing_page(response.text, str(response.url), category["name"])
        if page["entries_on_page"] not in (MAX_ITEMS_PER_PAGE, category["count"]):
            report["issues"].append(f"listing_entries_switch_failed:{page['entries_on_page']}")
        walked_rows = list(page["rows"])
        expected_pages = page["total_pages"]
        report["pages"].append({
            "page": page["current_page"],
            "total_pages": page["total_pages"],
            "rows": page["entries_on_page"],
            "job_ids": [row["job_id"] for row in page["rows"]],
        })
        report["issues"].extend(page["issues"])
        while page["current_page"] < page["total_pages"]:
            previous_page = page["current_page"]
            state = form_state(response.text)
            state[NEXT_CONTROL] = "Next"
            response = await self.http.post(url, data=state, kind="listing",
                                            extra={"category": category["name"], "mode": "next"})
            page = parse_listing_page(response.text, str(response.url), category["name"])
            if page["current_page"] != previous_page + 1:
                report["issues"].append(
                    f"listing_pager_did_not_advance:{previous_page}->{page['current_page']}"
                )
                break
            if page["total_pages"] != expected_pages:
                report["issues"].append(
                    f"listing_total_pages_changed:{expected_pages}->{page['total_pages']}"
                )
                expected_pages = page["total_pages"]
            report["pages"].append({
                "page": page["current_page"],
                "total_pages": page["total_pages"],
                "rows": page["entries_on_page"],
                "job_ids": [row["job_id"] for row in page["rows"]],
            })
            report["issues"].extend(page["issues"])
            walked_rows.extend(page["rows"])
        walked_ids = {row["job_id"] for row in walked_rows}
        extra_default_rows = [row for row in initial_page_rows if row["job_id"] not in walked_ids]
        rows = list(extra_default_rows) + walked_rows
        for row in rows:
            row["category_name"] = category["name"]
        report["walked_rows"] = len(walked_rows)
        report["rows"] = len(walked_rows)
        report["default_view_extra_rows"] = len(extra_default_rows)
        report["default_view_extra_ids"] = [row["job_id"] for row in extra_default_rows]
        report["rows_including_default_view_extras"] = len(rows)
        row_ids = [row["job_id"] for row in rows]
        report["unique_rows"] = len(set(row_ids))
        report["duplicate_rows"] = report["rows"] - report["unique_rows"]
        report["duplicate_ids_sample"] = [
            identity for identity, count in __import__("collections").Counter(row_ids).items()
            if count > 1
        ][:20]
        report["first_job_id"] = rows[0]["job_id"] if rows else None
        report["last_job_id"] = rows[-1]["job_id"] if rows else None
        report["expected_total_pages_at_25"] = (category["count"] + MAX_ITEMS_PER_PAGE - 1) // MAX_ITEMS_PER_PAGE
        report["last_page_rows"] = (
            category["count"] - MAX_ITEMS_PER_PAGE * (report["expected_total_pages_at_25"] - 1)
            if category["count"] else 0
        )
        if report["rows"] != category["count"]:
            report["issues"].append(
                f"listing_row_count_does_not_match_advertised:{report['rows']}!={category['count']}"
            )
        if report["expected_total_pages_at_25"] != expected_pages:
            report["issues"].append(
                f"listing_page_count_does_not_match_advertised:{expected_pages}"
                f"!={report['expected_total_pages_at_25']}"
            )
        report["seconds"] = max(0.0, time.perf_counter() - started)
        report["issues"] = list(dict.fromkeys(report["issues"]))
        return rows, report

    async def _fetch_specialized_list(self, name: str, path: str,
                                      known_functions: set[str] | None = None):
        """Walk an official spotlight list whose rows have no detail page."""
        started = time.perf_counter()
        url = urljoin(OFFICIAL_ENTRY, path.lstrip("/"))
        report = {
            "name": name,
            "path": path,
            "url": url,
            "pages": [],
            "issues": [],
        }
        response = await self.http.get(url, kind="specialized",
                                       extra={"list": name, "mode": "page1"})
        page = parse_specialized_page(response.text, str(response.url), name, known_functions)
        report["pager_at_10"] = page["total_pages"]
        report["initial_entries"] = page["entries_on_page"]
        report["issues"].extend(page["issues"])

        def attach_page_evidence(page_value, response_value):
            from hashlib import sha256
            digest = sha256(response_value.content).hexdigest()
            for row in page_value["rows"]:
                row["response_sha256"] = digest
                row["response_bytes"] = len(response_value.content)
                row["page_number"] = page_value["current_page"]
                row["page_url"] = str(response_value.url)

        attach_page_evidence(page, response)
        state = form_state(response.text)
        state[ENTRIES_CONTROL] = str(MAX_ITEMS_PER_PAGE)
        state["__EVENTTARGET"] = ENTRIES_CONTROL
        state["__EVENTARGUMENT"] = ""
        response = await self.http.post(url, data=state, kind="specialized",
                                        extra={"list": name, "mode": "entries25"})
        page = parse_specialized_page(response.text, str(response.url), name, known_functions)
        attach_page_evidence(page, response)
        if page["entries_on_page"] == 0 or page["entries_on_page"] > MAX_ITEMS_PER_PAGE:
            report["issues"].append(f"specialized_entries_switch_failed:{page['entries_on_page']}")
        rows = list(page["rows"])
        expected_pages = page["total_pages"]
        report["pages"].append({
            "page": page["current_page"],
            "total_pages": page["total_pages"],
            "rows": page["entries_on_page"],
            "job_ids": [row["job_id"] for row in page["rows"]],
        })
        report["issues"].extend(page["issues"])
        while page["current_page"] < page["total_pages"]:
            previous_page = page["current_page"]
            state = form_state(response.text)
            state[NEXT_CONTROL] = "Next"
            response = await self.http.post(url, data=state, kind="specialized",
                                            extra={"list": name, "mode": "next"})
            page = parse_specialized_page(response.text, str(response.url), name, known_functions)
            attach_page_evidence(page, response)
            if page["current_page"] != previous_page + 1:
                report["issues"].append(
                    f"specialized_pager_did_not_advance:{previous_page}->{page['current_page']}"
                )
                break
            if page["total_pages"] != expected_pages:
                report["issues"].append(
                    f"specialized_total_pages_changed:{expected_pages}->{page['total_pages']}"
                )
                expected_pages = page["total_pages"]
            report["pages"].append({
                "page": page["current_page"],
                "total_pages": page["total_pages"],
                "rows": page["entries_on_page"],
                "job_ids": [row["job_id"] for row in page["rows"]],
            })
            report["issues"].extend(page["issues"])
            rows.extend(page["rows"])
        report["rows"] = len(rows)
        report["first_job_id"] = rows[0]["job_id"] if rows else None
        report["last_job_id"] = rows[-1]["job_id"] if rows else None
        report["pages_at_25"] = len(report["pages"])
        if report["pages_at_25"] != page["total_pages"]:
            report["issues"].append(
                f"specialized_page_count_mismatch:{report['pages_at_25']}!={page['total_pages']}"
            )
        report["last_page_rows"] = (
            len(rows) - MAX_ITEMS_PER_PAGE * (len(report["pages"]) - 1)
        )
        max_rows = report["pager_at_10"] * 10
        if report["rows"] > max_rows:
            report["issues"].append(
                f"specialized_rows_exceed_pager_bound:{report['rows']}>{max_rows}"
            )
        if report["rows"] <= (report["pager_at_10"] - 1) * 10:
            report["issues"].append(
                f"specialized_rows_below_pager_bound:{report['rows']}"
                f"<={(report['pager_at_10'] - 1) * 10}"
            )
        if report["first_job_id"] is None:
            report["issues"].append("specialized_list_has_no_public_rows")
        report["seconds"] = max(0.0, time.perf_counter() - started)
        report["issues"] = list(dict.fromkeys(report["issues"]))
        return rows, report

    def _detail_from_html(self, listing, html, response_url, response_sha256,
                          response_bytes, status=200):
        """Build the exact Job from a saved or freshly fetched detail page."""
        parsed = parse_detail_page(html, response_url, listing)
        detail = {
            **parsed,
            "response_sha256": response_sha256,
            "response_bytes": response_bytes,
            "status": status,
        }
        parsed = detail
        warnings = []
        if parsed["description_unavailable"]:
            warnings.append("publisher_empty_description")
        if parsed["date_issue"]:
            warnings.append(parsed["date_issue"])
        sections_by_key = {section["key"]: section for section in parsed["sections"]}
        responsibilities = sections_by_key.get("responsibilities")
        qualifications = {
            key: sections_by_key[key]
            for key in ("education", "experience", "skills")
            if key in sections_by_key
        } or None
        raw = {
            "listing": listing,
            "detail": parsed,
            "detail_source": "exact frmjobdescription.aspx page linked by the official function list",
            "employer_provenance": (
                "Reliance Jio Infocomm Limited is declared as the organization of this official "
                "careers portal on its home-page JSON-LD; the value is not repeated per posting."
            ),
        }
        result = self.job(
            raw,
            title=parsed["title"],
            url=listing["detail_url"],
            job_id=parsed["job_id"],
            requisition_id=None,
            apply_url=listing["detail_url"],
            description=parsed["description_html"],
            method="jio.careers.frmjobdescription.sections",
            description_url=listing["detail_url"],
            locations=[parsed["location"]] if parsed["location"] else [],
            department=parsed["function"] or None,
            employment_type=None,
            workplace_type=None,
            date_posted=parsed["date_posted"],
            date_posted_raw=parsed["posted_date_raw"] or None,
            date_updated=None,
            valid_through=None,
            salary=None,
            responsibilities=[responsibilities] if responsibilities else None,
            qualifications=qualifications,
            employer="Reliance Jio Infocomm Limited",
            language="en",
            warnings=warnings,
        )
        result.description_text = parsed["description_text"]
        if parsed["description_unavailable"]:
            result.description_unavailable = True
        for issue in parsed["issues"]:
            if issue.startswith(("detail_function_mismatch", "detail_location_mismatch",
                                 "detail_posted_date_mismatch", "detail_missing_section",
                                 "detail_section_label_changed")):
                self.result.errors.append(f"jio_detail_source_mismatch:{parsed['job_id']}:{issue}")
                result.warnings.append(issue)
        result.company_scope_verified = self.company.scope == "company"
        return result

    async def _detail(self, job):
        listing = job.raw["listing"]
        response = await self.http.get(listing["detail_url"], kind="detail",
                                       extra={"job_id": listing["job_id"]})
        from hashlib import sha256

        return self._detail_from_html(
            listing,
            response.text,
            str(response.url),
            sha256(response.content).hexdigest(),
            len(response.content),
            response.status_code,
        )

    def _specialized_job(self, row):
        """Build one listing-only spotlight posting (no per-job body published)."""
        date_posted, date_issue = parse_posted_date(row["posted_date_raw"])
        detail = {
            "job_id": row["job_id"],
            "title": row["title"],
            "posted_date_raw": row["posted_date_raw"],
            "date_posted": date_posted,
            "date_issue": date_issue,
            "function": row["function"],
            "location": row["location"],
            "labels": {},
            "sections": [],
            "description_html": "",
            "description_text": "",
            "description_normalized": "",
            "description_normalized_chars": 0,
            "description_fnv1a64": fnv1a64(""),
            "description_unavailable": True,
            "apply_controls": [{
                "name": row.get("apply_control_name"),
                "value": row.get("apply_control_value"),
            }],
            "language_options": [],
            "coordinates": {},
            "query": {},
            "query_raw": "",
            "issues": [],
            "response_sha256": row.get("response_sha256"),
            "response_bytes": row.get("response_bytes"),
            "status": 200,
            "source_kind": "specialized_listing",
        }
        warnings = [
            "publisher_listing_only_no_detail_page",
            "publisher_empty_description",
        ]
        if date_issue:
            warnings.append(date_issue)
        job = self.job(
            {
                "listing": row,
                "detail": detail,
                "detail_source": (
                    "Official home-page spotlight list row; this list publishes no exact "
                    "frmjobdescription.aspx page or per-job role body."
                ),
                "specialized_listing": {
                    "name": row["list_name"],
                    "url": row["listing_url"],
                    "page_number": row.get("page_number"),
                    "submit_interest_control": row.get("apply_control_name"),
                },
                "employer_provenance": (
                    "Reliance Jio Infocomm Limited is declared as the organization of this "
                    "official careers portal on its home-page JSON-LD."
                ),
            },
            title=row["title"],
            url=row["listing_url"],
            job_id=row["job_id"],
            requisition_id=None,
            apply_url=row["listing_url"],
            description="",
            method="jio.careers.specialized_listing.row_only",
            description_url=row["listing_url"],
            locations=[row["location"]] if row["location"] else [],
            department=row["function"] or None,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=row["posted_date_raw"] or None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer="Reliance Jio Infocomm Limited",
            language="en",
            warnings=warnings,
        )
        job.description_text = ""
        job.description_unavailable = True
        self.result.validate_job(job)
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": CATEGORIES_URL,
            "listing_authority": (
                "The complete function taxonomy on Jio's own frmJobCategories.aspx Job "
                "Opportunities page, plus the three public job spotlight lists linked from the "
                "official home page (PWD, Jio Point, Jio Certified Freelancer Technician)."
            ),
            "detail_method": (
                "Each exact frmjobdescription.aspx page linked by the official function list; "
                "the seven visible detail fields are retained verbatim. Spotlight list rows "
                "publish no per-job detail page and are retained as listing-only postings."
            ),
            "request_scope": (
                "Only careers.jio.com's own category, function-list, spotlight-list and detail "
                "routes; no ATS board, parent-company board, aggregator or guessed slug is queried."
            ),
        })
        try:
            entry_started = time.perf_counter()
            entry_response = await self.http.get(OFFICIAL_ENTRY, kind="entry", extra={"phase": "official_entry"})
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            entry.update({
                "status": entry_response.status_code,
                "seconds": max(0.0, time.perf_counter() - entry_started),
                "response_bytes": len(entry_response.content),
            })
            self.result.metadata["initial_official_entry"] = entry
            categories = await self._fetch_categories()
        except Exception as exc:
            self.result.errors.append(f"Jio source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_categories"] = categories
        if categories["issues"]:
            self.result.errors.extend(categories["issues"])
        categories_to_walk = [
            category for category in categories["categories"]
            if self.category_filter is None or category["name"] in self.category_filter
        ]
        if self.category_filter is not None:
            missing = self.category_filter - {category["name"] for category in categories["categories"]}
            if missing:
                self.result.errors.append(f"jio_requested_category_missing:{sorted(missing)}")
            self.result.metadata["category_filter"] = sorted(self.category_filter)
            self.result.metadata["filtered_smoke_run"] = True
        main_advertised_total = sum(category["count"] for category in categories_to_walk)

        listing_started = time.perf_counter()
        listing_rows, category_reports = [], []
        for category in categories_to_walk:
            if category["count"] <= 0:
                category_reports.append({
                    "code": category["code"], "name": category["name"],
                    "advertised_count": 0, "rows": 0, "skipped_empty_category": True,
                })
                continue
            try:
                rows, report = await self._fetch_category(category)
            except Exception as exc:
                report = {
                    "code": category["code"], "name": category["name"],
                    "advertised_count": category["count"], "rows": 0,
                    "issues": [f"category_walk_failed:{type(exc).__name__}: {exc}"],
                }
                rows = []
                self.result.errors.append(
                    f"Jio function list failed [{category['name']}]: {type(exc).__name__}: {exc}"
                )
            category_reports.append(report)
            self.result.errors.extend(
                f"Jio [{category['name']}]: {issue}" for issue in report.get("issues", [])
            )
            listing_rows.extend(rows)

        # The official lists repeat some postings on later pages. Deduplicate by
        # the published numeric posting id, while keeping the raw row count and
        # exact duplicate evidence.
        unique_rows, seen_ids = [], set()
        duplicate_ids = []
        for row in listing_rows:
            if row["job_id"] in seen_ids:
                duplicate_ids.append(row["job_id"])
                continue
            seen_ids.add(row["job_id"])
            unique_rows.append(row)
        category_sets = {}
        for report in category_reports:
            ids = set()
            for page in report.get("pages") or []:
                ids.update(page.get("job_ids") or [])
            category_sets[report.get("name")] = ids
        cross_category = set()
        id_categories = {}
        for name, ids in category_sets.items():
            for identity in ids:
                id_categories.setdefault(identity, set()).add(name)
        for identity, names in id_categories.items():
            if len(names) > 1:
                cross_category.add(identity)
        if cross_category:
            self.result.warnings.append(
                f"jio_posting_ids_appear_in_multiple_function_lists:{len(cross_category)}"
            )
        if duplicate_ids:
            self.result.warnings.append(
                f"jio_duplicate_listing_rows_deduplicated:{len(duplicate_ids)}"
            )
        self.result.timings["listing_phase_seconds"] = max(0.0, time.perf_counter() - listing_started)
        self.result.metadata["listing"] = {
            "category_reports": category_reports,
            "categories_reported": len(category_reports),
            "advertised_rows": main_advertised_total,
            "listed_rows": len(listing_rows),
            "unique_job_ids": len(unique_rows),
            "duplicate_listing_rows": len(duplicate_ids),
            "duplicate_ids_sample": sorted(set(duplicate_ids))[:50],
            "cross_category_duplicate_ids": sorted(cross_category)[:50],
            "cross_category_duplicate_count": len(cross_category),
            "entries_per_page": MAX_ITEMS_PER_PAGE,
            "order": "categories in official page order, then listing page order",
            "listing_wall_seconds": round(self.result.timings["listing_phase_seconds"], 6),
        }
        if len(listing_rows) < main_advertised_total:
            self.result.errors.append(
                f"jio_function_listing_row_count_mismatch:{len(listing_rows)}!={main_advertised_total}"
            )

        # Official home-page spotlight lists.  These publish job rows and a
        # Submit Interest application but no per-job description page.
        specialized_started = time.perf_counter()
        specialized_rows, specialized_reports = [], []
        known_functions = {category["name"] for category in categories["categories"]}
        if self.category_filter is None or self.include_specialized:
            for list_name, list_path in SPECIALIZED_LISTS:
                try:
                    rows, report = await self._fetch_specialized_list(
                        list_name, list_path, known_functions
                    )
                except Exception as exc:
                    report = {
                        "name": list_name, "path": list_path,
                        "url": urljoin(OFFICIAL_ENTRY, list_path.lstrip("/")),
                        "rows": 0, "pages": [],
                        "issues": [f"specialized_walk_failed:{type(exc).__name__}: {exc}"],
                    }
                    rows = []
                    self.result.errors.append(
                        f"Jio spotlight list failed [{list_name}]: {type(exc).__name__}: {exc}"
                    )
                specialized_reports.append(report)
                self.result.errors.extend(
                    f"Jio spotlight [{list_name}]: {issue}" for issue in report.get("issues", [])
                )
                specialized_rows.extend(rows)
        else:
            self.result.metadata["specialized_skipped_for_smoke_filter"] = True
        self.result.timings["specialized_listing_phase_seconds"] = max(
            0.0, time.perf_counter() - specialized_started
        )

        specialized_unique, specialized_duplicates, specialized_overlap = [], [], []
        seen_specialized = set()
        for row in specialized_rows:
            identity = row["job_id"]
            if identity in seen_specialized:
                specialized_duplicates.append(identity)
                continue
            seen_specialized.add(identity)
            if identity in seen_ids:
                specialized_overlap.append(identity)
                continue
            specialized_unique.append(row)
        if specialized_duplicates:
            self.result.errors.append(
                f"jio_duplicate_job_ids_across_spotlight_lists:{len(specialized_duplicates)}"
            )
        union_total = len(unique_rows) + len(specialized_unique)
        self.result.expected_total = union_total
        self.result.metadata["specialized"] = {
            "lists": specialized_reports,
            "rows": len(specialized_rows),
            "unique_rows": len(seen_specialized),
            "new_rows": len(specialized_unique),
            "overlap_with_function_taxonomy": len(specialized_overlap),
            "overlap_ids": specialized_overlap,
            "duplicate_ids": specialized_duplicates,
            "entries_per_page": MAX_ITEMS_PER_PAGE,
            "union_expected_total": union_total,
            "listing_wall_seconds": round(self.result.timings["specialized_listing_phase_seconds"], 6),
        }

        # Stub jobs preserve official listing order before detail hydration.
        jobs = [
            self.job(
                {"listing": row},
                title=row["title"],
                url=row["detail_url"],
                job_id=row["job_id"],
                requisition_id=None,
                apply_url=row["detail_url"],
                description="",
                date_posted=None,
                date_posted_raw=row["posted_date_raw"],
                locations=[row["location"]],
                department=row["function"],
                employer="Reliance Jio Infocomm Limited",
                language="en",
            )
            for row in unique_rows
        ]
        hydrated = await self.hydrate(jobs, self._detail)

        specialized_jobs = [self._specialized_job(row) for row in specialized_unique]

        self.result.jobs = list(hydrated) + specialized_jobs
        main_jobs = [job for job in self.result.jobs if job.raw.get("detail", {}).get("source_kind") != "specialized_listing"]
        detail_failures = [job for job in main_jobs if not job.raw.get("detail")]
        if detail_failures:
            for job in detail_failures:
                self.result.errors.append(f"jio_detail_page_not_captured:{job.job_id}:{job.url}")
        if len(self.result.jobs) != union_total:
            self.result.errors.append(
                f"jio_hydrated_count_mismatch:{len(self.result.jobs)}!={union_total}"
            )

        # Final live recheck of the public taxonomy and each list's page-1 fingerprint.
        recheck_started = time.perf_counter()
        try:
            final_categories = await self._fetch_categories()
            self.result.metadata["final_categories"] = final_categories
            if final_categories["issues"]:
                self.result.errors.extend(final_categories["issues"])
            initial_by_code = {row["code"]: row for row in categories["categories"]}
            final_by_code = {row["code"]: row for row in final_categories["categories"]}
            changed = []
            for code in sorted(set(initial_by_code) | set(final_by_code)):
                before, after = initial_by_code.get(code), final_by_code.get(code)
                if before is None or after is None:
                    changed.append({"code": code, "before": before, "after": after})
                    continue
                if (before["count"], before["name"], before["url"]) != (
                        after["count"], after["name"], after["url"]):
                    changed.append({
                        "code": code, "name": after["name"],
                        "before_count": before["count"], "after_count": after["count"],
                        "before_url": before["url"], "after_url": after["url"],
                    })
            if changed:
                self.result.errors.append("jio_function_taxonomy_changed_during_collection")
            page1_rechecks = []
            for report in category_reports:
                if report.get("skipped_empty_category"):
                    continue
                url = next(
                    row["url"] for row in categories["categories"] if row["code"] == report["code"]
                )
                response = await self.http.get(url, kind="listing",
                                               extra={"category": report["name"], "mode": "final_page1"})
                page = parse_listing_page(response.text, str(response.url), report["name"])
                expected_pages = (report["advertised_count"] + 9) // 10
                first_id = page["rows"][0]["job_id"] if page["rows"] else None
                entry = {
                    "code": report["code"],
                    "name": report["name"],
                    "current_page": page["current_page"],
                    "total_pages_at_10": page["total_pages"],
                    "expected_total_pages_at_10": expected_pages,
                    "first_job_id": first_id,
                    "initial_first_job_id": report.get("first_job_id"),
                    "same_first_job": first_id == report.get("first_job_id"),
                    "same_page_count": page["total_pages"] == expected_pages,
                }
                page1_rechecks.append(entry)
                if not entry["same_page_count"]:
                    self.result.errors.append(
                        f"jio_function_page_count_changed:{report['name']}:"
                        f"{expected_pages}->{page['total_pages']}"
                    )
                if not entry["same_first_job"]:
                    self.result.errors.append(
                        f"jio_function_first_job_changed:{report['name']}:"
                        f"{report.get('first_job_id')}->{first_id}"
                    )
            specialized_rechecks = []
            for report in specialized_reports:
                if not report.get("pager_at_10"):
                    continue
                response = await self.http.get(report["url"], kind="specialized",
                                               extra={"list": report["name"], "mode": "final_page1"})
                page = parse_specialized_page(response.text, str(response.url),
                                              report["name"], known_functions)
                first_id = page["rows"][0]["job_id"] if page["rows"] else None
                entry = {
                    "name": report["name"],
                    "current_page": page["current_page"],
                    "total_pages_at_10": page["total_pages"],
                    "initial_total_pages_at_10": report.get("pager_at_10"),
                    "first_job_id": first_id,
                    "initial_first_job_id": report.get("first_job_id"),
                    "same_first_job": first_id == report.get("first_job_id"),
                    "same_page_count": page["total_pages"] == report.get("pager_at_10"),
                }
                specialized_rechecks.append(entry)
                if not entry["same_page_count"]:
                    self.result.errors.append(
                        f"jio_spotlight_page_count_changed:{report['name']}:"
                        f"{report.get('pager_at_10')}->{page['total_pages']}"
                    )
                if not entry["same_first_job"]:
                    self.result.errors.append(
                        f"jio_spotlight_first_job_changed:{report['name']}:"
                        f"{report.get('first_job_id')}->{first_id}"
                    )
            self.result.metadata["final_page1_rechecks"] = page1_rechecks
            self.result.metadata["final_specialized_rechecks"] = specialized_rechecks
            self.result.metadata["taxonomy_changes"] = changed
        except Exception as exc:
            self.result.errors.append(f"Jio final taxonomy recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - recheck_started)

        details = self.result.metadata.get("details", {})
        detail_failed = details.get("failed", 0)
        verified = sum(job.description_verified for job in self.result.jobs)
        unavailable = sum(job.description_unavailable for job in self.result.jobs)
        self.result.metadata.update({
            "job_ids": [job.job_id for job in self.result.jobs],
            "jobs_collected": len(self.result.jobs),
            "descriptions_verified": verified,
            "descriptions_unavailable": unavailable,
            "detail_failures": detail_failed,
            "main_jobs": len(unique_rows),
            "specialized_jobs": len(specialized_jobs),
            "listing_pages_walked": (
                sum(len(report.get("pages") or []) for report in category_reports)
                + sum(len(report.get("pages") or []) for report in specialized_reports)
            ),
        })
        self.result.listing_complete = bool(
            not self.result.errors
            and len(self.result.jobs) == union_total
            and len(listing_rows) >= main_advertised_total
            and detail_failed == 0
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.metadata["source_collection_wall_seconds"] = round(
            self.result.timings["source_collection_wall_seconds"], 6
        )
        return self.result
