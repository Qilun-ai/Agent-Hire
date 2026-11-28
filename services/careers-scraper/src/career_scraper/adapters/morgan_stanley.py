"""Morgan Stanley's published careers journey: global Eightfold board plus the
regional China and Korea listings that the same official region selector routes
to.

The official entry ``https://www.morganstanley.com/people-opportunities`` links
"Search All Opportunities" to
``https://www.morganstanley.com/careers/career-opportunities-search``.  That
page's company-shipped ``opportunity-aggregate`` script reads
``/content/dam/msdotcom/appdata/filter-metadata-location.json`` and sends each
region to the URL published there:

* Americas, EMEA, Hong Kong SAR, India, Japan and All Others ->
  ``https://morganstanley.eightfold.ai/careers?source=mscom``.
* Mainland China -> ``https://careers.morganstanley.com.cn`` (SuccessFactors
  Career Site Builder).
* Republic of Korea -> the company's Workday External site filtered to country
  id ``7a5a2aadf9d34086a2bfbfd408bc28da``.

The Eightfold board is the unfiltered company-scoped listing (10 rows per
request, ``start`` pagination, count 1207 at review time).  Its detail endpoint
``/api/pcsx/position_details`` exposes the complete public ``jobDescription``
field, internal position id, Workday requisition id, locations, department,
workplace type, employment type, job level, posted timestamp and the Workday
apply URL.  The China site exposes its own 18-post listing and full description
on the job page; the Korea Workday facet exposes 8 postings with full
``jobPostingInfo.jobDescription`` and exact ``startDate``.

Only these three listings are collected.  The unfiltered Workday board, the
AEM ``careerservice/resultset.json`` legacy servlet (which returns an empty
``opportunity=ep`` set), account/talent-community routes and aggregators are not
membership authorities.  The Workday unfiltered total is read once only as a
cross-check: 1215 = Eightfold 1207 + Korea 8, and the China 18 are not on that
board.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://www.morganstanley.com/people-opportunities"
SEARCH_PAGE_URL = "https://www.morganstanley.com/careers/career-opportunities-search"
LOCATION_METADATA_URL = (
    "https://www.morganstanley.com/content/dam/msdotcom/appdata/filter-metadata-location.json"
)
EIGHTFOLD_ORIGIN = "https://morganstanley.eightfold.ai"
EIGHTFOLD_HOST = "morganstanley.eightfold.ai"
EIGHTFOLD_DOMAIN = "morganstanley.com"
EIGHTFOLD_BOARD_URL = EIGHTFOLD_ORIGIN + "/careers"
EIGHTFOLD_SEARCH_URL = EIGHTFOLD_ORIGIN + "/api/pcsx/search"
EIGHTFOLD_DETAIL_URL = EIGHTFOLD_ORIGIN + "/api/pcsx/position_details"
EIGHTFOLD_PAGE_SIZE = 10
EIGHTFOLD_SORT = "timestamp"

CHINA_ORIGIN = "https://careers.morganstanley.com.cn"
CHINA_SEARCH_URL = CHINA_ORIGIN + "/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=0"
CHINA_JOBS_URL = CHINA_ORIGIN + "/services/recruiting/v1/jobs"
CHINA_PAGE_SIZE = 10

WORKDAY_ORIGIN = "https://ms.wd5.myworkdayjobs.com"
WORKDAY_JOBS_URL = WORKDAY_ORIGIN + "/wday/cxs/ms/External/jobs"
WORKDAY_DETAIL_PREFIX = WORKDAY_ORIGIN + "/wday/cxs/ms/External"
WORKDAY_KOREA_COUNTRY_ID = "7a5a2aadf9d34086a2bfbfd408bc28da"
WORKDAY_PAGE_SIZE = 20

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
HTML_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
EIGHTFOLD_HEADERS = {
    **BROWSER_HEADERS,
    "Referer": EIGHTFOLD_BOARD_URL + "?source=mscom",
    "Origin": EIGHTFOLD_ORIGIN,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
CHINA_HEADERS = {
    **BROWSER_HEADERS,
    "Referer": CHINA_SEARCH_URL,
    "Origin": CHINA_ORIGIN,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
WORKDAY_HEADERS = {
    **BROWSER_HEADERS,
    "Referer": WORKDAY_ORIGIN + "/en-US/External?Location_Country=" + WORKDAY_KOREA_COUNTRY_ID,
    "Origin": WORKDAY_ORIGIN,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "pre",
    "hr", "dl", "dt", "dd", "header", "footer", "main", "nav",
})
_DATE_US = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*$")


class FatalSourceError(RuntimeError):
    """A 403/429/challenge means stop scheduling further detail requests."""


def _space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def visible_text(html: str | None) -> str:
    """Block-aware visible text, close to a browser's collapsed innerText."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        block = node.name in BLOCK_TAGS
        if block:
            chunks.append("\n")
        for child in node.children:
            walk(child)
        if block:
            chunks.append("\n")

    walk(soup)
    return _space("".join(chunks))


def visible_lines(html: str | None) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        block = node.name in BLOCK_TAGS
        if block:
            chunks.append("\n")
        for child in node.children:
            walk(child)
        if block:
            chunks.append("\n")

    walk(soup)
    value = "".join(chunks).replace("\xa0", " ")
    return [line.strip() for line in re.split(r"\n+", value) if line.strip()]


def epoch_date(value) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


def us_date(value: str | None) -> str | None:
    match = _DATE_US.match(value or "")
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _first_list(value) -> list:
    return value if isinstance(value, list) else []


def _single_or_list(values: list):
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.morganstanley.com", "morganstanley.com"}
            or parsed.path.rstrip("/") != "/people-opportunities"):
        raise ValueError("Morgan Stanley official careers entry is not the inspected people-opportunities page")
    soup = BeautifulSoup(html, "lxml")
    title = _space(soup.title.get_text(" ", strip=True) if soup.title else "")
    og_title = og_url = og_site = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:url":
            og_url = meta.get("content")
        elif meta.get("property") == "og:site_name":
            og_site = meta.get("content")
    if "Morgan Stanley" not in title and og_site != "Morgan Stanley":
        raise ValueError("Morgan Stanley official careers page identity changed")
    if og_url and urlsplit(og_url).path.rstrip("/") != "/people-opportunities":
        raise ValueError("Morgan Stanley official careers og:url changed")

    search_links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        target = urljoin(response_url, href)
        path = urlsplit(target).path.rstrip("/")
        if path == "/careers/career-opportunities-search":
            search_links.append({
                "url": target,
                "label": _space(anchor.get_text(" ", strip=True)),
            })
    if not any(item["url"].rstrip("/") == SEARCH_PAGE_URL for item in search_links):
        raise ValueError("Morgan Stanley official careers page no longer publishes the All Opportunities search")
    return {
        "url": response_url,
        "title": title,
        "og_title": og_title,
        "og_url": og_url,
        "og_site_name": og_site,
        "search_links": search_links,
    }


def parse_search_page(html: str, response_url: str = SEARCH_PAGE_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.morganstanley.com", "morganstanley.com"}
            or parsed.path.rstrip("/") != "/careers/career-opportunities-search"):
        raise ValueError("Morgan Stanley search page came from an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = _space(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Career Opportunities" not in title and "Morgan Stanley" not in title:
        raise ValueError("Morgan Stanley career opportunities search page identity changed")
    if not soup.select_one(".cmp-opportunity-aggregate"):
        raise ValueError("Morgan Stanley search page no longer publishes the opportunity aggregate component")
    scripts = [
        urljoin(response_url, script.get("src"))
        for script in soup.select("script[src]")
        if "opportunity-aggregate" in (script.get("src") or "")
    ]
    if not scripts:
        raise ValueError("Morgan Stanley search page no longer loads its opportunity aggregate script")
    return {
        "url": response_url,
        "title": title,
        "aggregate_component": True,
        "aggregate_scripts": scripts,
    }


def parse_location_metadata(value) -> dict:
    """Validate the region selector's published destinations and scope."""
    if not isinstance(value, list) or not value:
        raise ValueError("Morgan Stanley location metadata is not a non-empty list")
    entries = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Morgan Stanley location metadata row is not an object")
        name, url, opened = item.get("name"), item.get("url"), item.get("open")
        if not isinstance(name, str) or not name.strip() or not isinstance(url, str):
            raise ValueError("Morgan Stanley location metadata row is missing name/url")
        entries.append({"name": name.strip(), "url": url.strip(), "open": opened})
    allowed_hosts = {EIGHTFOLD_HOST, "careers.morganstanley.com.cn", "ms.wd5.myworkdayjobs.com"}
    unexpected = sorted({
        item["url"] for item in entries
        if (urlsplit(item["url"]).hostname or "") not in allowed_hosts
    })
    if unexpected:
        raise ValueError(f"Morgan Stanley location metadata has an unexpected destination: {unexpected}")
    eightfold = sorted({item["url"] for item in entries
                        if urlsplit(item["url"]).hostname == EIGHTFOLD_HOST})
    china = sorted({item["url"] for item in entries
                    if urlsplit(item["url"]).hostname == "careers.morganstanley.com.cn"})
    workday = sorted({item["url"] for item in entries
                      if urlsplit(item["url"]).hostname == "ms.wd5.myworkdayjobs.com"})
    if eightfold != [EIGHTFOLD_BOARD_URL + "?source=mscom"]:
        raise ValueError(f"Morgan Stanley location metadata Eightfold destination changed: {eightfold}")
    if china != [CHINA_ORIGIN]:
        raise ValueError(f"Morgan Stanley location metadata China destination changed: {china}")
    if len(workday) != 1 or "Location_Country=" + WORKDAY_KOREA_COUNTRY_ID not in workday[0]:
        raise ValueError(f"Morgan Stanley location metadata Korea Workday destination changed: {workday}")
    return {
        "entries": entries,
        "eightfold_urls": eightfold,
        "china_urls": china,
        "workday_urls": workday,
        "korea_country_id": WORKDAY_KOREA_COUNTRY_ID,
        "eightfold_regions": [
            item["name"] for item in entries if urlsplit(item["url"]).hostname == EIGHTFOLD_HOST
        ],
        "china_regions": [
            item["name"] for item in entries if urlsplit(item["url"]).hostname == "careers.morganstanley.com.cn"
        ],
        "workday_regions": [
            item["name"] for item in entries if urlsplit(item["url"]).hostname == "ms.wd5.myworkdayjobs.com"
        ],
    }


def parse_eightfold_search(value, requested_start: int, page_size: int = EIGHTFOLD_PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Morgan Stanley Eightfold search response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Morgan Stanley Eightfold search returned status {value.get('status')!r}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Morgan Stanley Eightfold search response has no data block")
    count = data.get("count")
    positions = data.get("positions")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Morgan Stanley Eightfold search response has no integer count")
    if not isinstance(positions, list):
        raise ValueError("Morgan Stanley Eightfold search response has no positions list")
    if data.get("appliedFilters") not in (None, [], {}):
        raise ValueError(f"Morgan Stanley Eightfold search unexpectedly applied filters: {data.get('appliedFilters')}")
    expected = min(page_size, max(0, count - requested_start))
    issues = []
    if len(positions) != expected:
        issues.append(f"eightfold_row_count_mismatch:{len(positions)}!={expected}")
    seen = set()
    for position in positions:
        if not isinstance(position, dict):
            issues.append("eightfold_position_not_object")
            continue
        identity = position.get("id")
        if not isinstance(identity, (int, str)) or str(identity) == "":
            issues.append("eightfold_position_missing_id")
            continue
        if str(identity) in seen:
            issues.append(f"eightfold_duplicate_id:{identity}")
        seen.add(str(identity))
        if not isinstance(position.get("name"), str) or not position["name"].strip():
            issues.append(f"eightfold_position_missing_title:{identity}")
        if not isinstance(position.get("atsJobId"), str) or not position["atsJobId"].strip():
            issues.append(f"eightfold_position_missing_ats_id:{identity}")
        if position.get("positionUrl") != f"/careers/job/{identity}":
            issues.append(f"eightfold_position_url_changed:{identity}")
        if not isinstance(position.get("locations"), list) or not position["locations"]:
            issues.append(f"eightfold_position_missing_locations:{identity}")
        posted = position.get("postedTs")
        if posted is not None and not isinstance(posted, (int, float)):
            issues.append(f"eightfold_invalid_posted_ts:{identity}")
    return {
        "rows": positions,
        "count": count,
        "start": requested_start,
        "page_size": page_size,
        "sort_by": data.get("sortBy"),
        "issues": issues,
    }


def parse_eightfold_detail(value, position_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Morgan Stanley Eightfold detail response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Morgan Stanley Eightfold detail returned status {value.get('status')!r}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Morgan Stanley Eightfold detail response has no data block")
    if str(data.get("id")) != str(position_id):
        raise ValueError(f"Morgan Stanley Eightfold detail id changed: {data.get('id')} != {position_id}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("Morgan Stanley Eightfold detail has no title")
    if not isinstance(data.get("jobDescription"), str) or not data["jobDescription"].strip():
        raise ValueError("Morgan Stanley Eightfold detail has no jobDescription")
    if not isinstance(data.get("locations"), list) or not data["locations"]:
        raise ValueError("Morgan Stanley Eightfold detail has no locations")
    if data.get("postedTs") is not None and not isinstance(data.get("postedTs"), (int, float)):
        raise ValueError("Morgan Stanley Eightfold detail has an invalid postedTs")
    apply_action = (data.get("positionUserActions") or {}).get("applyAction") or {}
    apply_url = apply_action.get("applyUrl")
    if apply_url and not isinstance(apply_url, str):
        raise ValueError("Morgan Stanley Eightfold detail applyUrl is not a string")
    return data


def parse_china_listing(value, page_number: int, page_size: int = CHINA_PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Morgan Stanley China listing is not an object")
    total = value.get("totalJobs")
    rows = value.get("jobSearchResult")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Morgan Stanley China listing has no integer totalJobs")
    if not isinstance(rows, list):
        raise ValueError("Morgan Stanley China listing has no jobSearchResult list")
    expected = min(page_size, max(0, total - page_number * page_size))
    issues = []
    if len(rows) != expected:
        issues.append(f"china_row_count_mismatch:{len(rows)}!={expected}")
    parsed = []
    seen = set()
    for row in rows:
        response = row.get("response") if isinstance(row, dict) else None
        if not isinstance(response, dict):
            issues.append("china_row_not_object")
            continue
        identity = str(response.get("id") or "")
        if not identity:
            issues.append("china_row_missing_id")
            continue
        if identity in seen:
            issues.append(f"china_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(response.get("unifiedStandardTitle"), str) or not response["unifiedStandardTitle"].strip():
            issues.append(f"china_row_missing_title:{identity}")
        if not isinstance(response.get("unifiedUrlTitle"), str) or not response["unifiedUrlTitle"]:
            issues.append(f"china_row_missing_url_title:{identity}")
        start = us_date(response.get("unifiedStandardStart"))
        if response.get("unifiedStandardStart") and not start:
            issues.append(f"china_row_invalid_start:{identity}")
        end = us_date(response.get("unifiedStandardEnd"))
        if response.get("unifiedStandardEnd") and not end:
            issues.append(f"china_row_invalid_end:{identity}")
        if not isinstance(response.get("mfield1"), list):
            issues.append(f"china_row_missing_location:{identity}")
        parsed.append(response)
    return {
        "rows": parsed,
        "total": total,
        "page_number": page_number,
        "page_size": page_size,
        "issues": issues,
    }


def china_job_url(row: dict) -> str:
    return f"{CHINA_ORIGIN}/job/{row['unifiedUrlTitle']}/{row['id']}-en_US"


def parse_china_detail(html: str, row: dict, response_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one('[itemprop="title"]')
    desc_node = soup.select_one('[itemprop="description"]')
    if title_node is None or desc_node is None:
        raise ValueError("Morgan Stanley China job page no longer exposes its title/description")
    title = _space(title_node.get_text(" ", strip=True))
    if title != _space(row.get("unifiedStandardTitle")):
        raise ValueError(f"Morgan Stanley China title changed: {title!r}")
    description_html = desc_node.decode_contents()
    if not description_html.strip():
        raise ValueError("Morgan Stanley China job page has an empty description")
    labels = {}
    for token in soup.select(".joblayouttoken"):
        label = token.select_one(".joblayouttoken-label")
        if label:
            key = _space(label.get_text(" ", strip=True)).rstrip(":").strip()
            value = _space(token.get_text(" ", strip=True))
            if key:
                labels[key] = value[len(key) + 1:].strip() if value.startswith(key) else value
    apply_anchor = soup.select_one('.applylink a[href], a[href*="/talentcommunity/apply/"]')
    apply_url = urljoin(response_url, apply_anchor.get("href")) if apply_anchor else None
    return {
        "url": response_url,
        "title": title,
        "description_html": description_html,
        "labels": labels,
        "apply_url": apply_url,
        "page_title": _space(soup.title.get_text(" ", strip=True) if soup.title else ""),
    }


def parse_workday_listing(value, offset: int, limit: int = WORKDAY_PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Morgan Stanley Workday listing is not an object")
    total = value.get("total")
    rows = value.get("jobPostings")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Morgan Stanley Workday listing has no integer total")
    if not isinstance(rows, list):
        raise ValueError("Morgan Stanley Workday listing has no jobPostings list")
    expected = min(limit, max(0, total - offset))
    issues = []
    if len(rows) != expected:
        issues.append(f"workday_row_count_mismatch:{len(rows)}!={expected}")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("workday_row_not_object")
            continue
        path = row.get("externalPath")
        if not isinstance(path, str) or not path.startswith("/job/"):
            issues.append("workday_row_missing_external_path")
            continue
        if path in seen:
            issues.append(f"workday_duplicate_path:{path}")
        seen.add(path)
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            issues.append(f"workday_row_missing_title:{path}")
        if not isinstance(row.get("bulletFields"), list) or not row["bulletFields"]:
            issues.append(f"workday_row_missing_requisition:{path}")
    return {
        "rows": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "issues": issues,
    }


def parse_workday_detail(value, requisition_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Morgan Stanley Workday detail is not an object")
    info = value.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Morgan Stanley Workday detail has no jobPostingInfo")
    if str(info.get("jobReqId")) != str(requisition_id):
        raise ValueError(f"Morgan Stanley Workday requisition changed: {info.get('jobReqId')} != {requisition_id}")
    if not isinstance(info.get("jobDescription"), str) or not info["jobDescription"].strip():
        raise ValueError("Morgan Stanley Workday detail has no jobDescription")
    if not isinstance(info.get("title"), str) or not info["title"].strip():
        raise ValueError("Morgan Stanley Workday detail has no title")
    if not isinstance(info.get("startDate"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", info["startDate"]):
        raise ValueError("Morgan Stanley Workday detail has no exact startDate")
    if not isinstance(info.get("externalUrl"), str):
        raise ValueError("Morgan Stanley Workday detail has no externalUrl")
    return {
        "info": info,
        "hiring_organization": value.get("hiringOrganization") or {},
        "similar_jobs": value.get("similarJobs") or [],
    }


def apply_eightfold_detail(job, detail: dict, source_url: str):
    apply_action = (detail.get("positionUserActions") or {}).get("applyAction") or {}
    apply_url = apply_action.get("applyUrl")
    job.raw["detail"] = detail
    job.raw["detail_request_url"] = source_url
    job.raw["description_field"] = "data.jobDescription"
    job.raw["display_job_id"] = detail.get("displayJobId")
    job.raw["internal_position_id"] = str(detail.get("id"))
    job.raw["standardized_locations"] = detail.get("standardizedLocations") or []
    job.raw["job_level"] = detail.get("efcustomTextPcsPostingJobLevel") or []
    job.raw["time_type"] = detail.get("efcustomTextTextTimeType") or []
    job.raw["creationTs"] = detail.get("creationTs")
    job.raw["location_flexibility"] = detail.get("locationFlexibility")
    job.raw["apply_action_status"] = apply_action.get("status")
    job.description_html = detail["jobDescription"]
    job.description_text = text_html(job.description_html)
    job.description_method = "morganstanley.eightfold.position_details.jobDescription"
    job.description_source_url = source_url
    job.title = _space(detail.get("name"))
    job.apply_url = apply_url or None
    job.locations = list(detail.get("locations") or [])
    job.department = detail.get("department") or job.department
    job.workplace_type = detail.get("workLocationOption") or job.workplace_type
    types = _first_list(detail.get("efcustomTextTextTimeType"))
    job.employment_type = _single_or_list([_space(item) for item in types if _space(item)])
    levels = _first_list(detail.get("efcustomTextPcsPostingJobLevel"))
    job.experience = _single_or_list([_space(item) for item in levels if _space(item)])
    job.date_posted = epoch_date(detail.get("postedTs"))
    job.date_posted_raw = {
        "postedTs": detail.get("postedTs"),
        "postedTs_iso": (
            datetime.fromtimestamp(detail["postedTs"], timezone.utc).isoformat()
            if isinstance(detail.get("postedTs"), (int, float)) else None
        ),
        "creationTs_not_used": detail.get("creationTs"),
        "page_posted_date": None,
    }
    job.language = None
    job.employer = "Morgan Stanley"
    if not job.apply_url:
        job.warnings.append("missing_apply_url")
    return job


def apply_china_detail(job, detail: dict, source_url: str):
    job.raw["detail"] = detail
    job.raw["detail_request_url"] = source_url
    job.raw["description_field"] = "itemprop=description"
    job.raw["page_labels"] = detail["labels"]
    job.description_html = detail["description_html"]
    job.description_text = text_html(job.description_html)
    job.description_method = "morganstanley.china_successfactors.itemprop_description"
    job.description_source_url = source_url
    job.title = detail["title"]
    job.url = source_url
    job.apply_url = detail["apply_url"]
    page_start = detail["labels"].get("Posting Start Date")
    job.date_posted_raw = {
        **(job.date_posted_raw or {}),
        "page_posting_start": page_start,
        "page_posting_start_iso": us_date(page_start),
    }
    if job.date_posted and us_date(page_start) and job.date_posted != us_date(page_start):
        raise ValueError(
            f"Morgan Stanley China posting date changed: {job.date_posted} != {us_date(page_start)}"
        )
    if not job.apply_url:
        job.warnings.append("missing_apply_url")
    job.employer = "Morgan Stanley"
    return job


def apply_workday_detail(job, detail: dict, source_url: str):
    info = detail["info"]
    job.raw["detail"] = detail
    job.raw["detail_request_url"] = source_url
    job.raw["description_field"] = "jobPostingInfo.jobDescription"
    job.raw["job_posting_id"] = info.get("jobPostingId")
    job.raw["job_requisition_location"] = info.get("jobRequisitionLocation")
    job.raw["country"] = info.get("country")
    job.raw["can_apply"] = info.get("canApply")
    job.description_html = info["jobDescription"]
    job.description_text = text_html(job.description_html)
    job.description_method = "morganstanley.workday.jobPostingInfo.jobDescription"
    job.description_source_url = source_url
    job.title = _space(info.get("title"))
    job.url = info["externalUrl"]
    job.apply_url = info["externalUrl"].rstrip("/") + "/apply"
    job.locations = [info.get("location")] if info.get("location") else job.locations
    job.employment_type = info.get("timeType") or None
    job.employer = (detail.get("hiring_organization") or {}).get("name") or "Morgan Stanley"
    job.date_posted = info["startDate"]
    job.date_posted_raw = {
        "startDate": info.get("startDate"),
        "postedOn": info.get("postedOn"),
        "relative_posted_label_not_used": True,
    }
    job.workplace_type = None
    return job


class MorganStanley(Adapter):
    """Collect the three company-scoped listings in the published region journey."""

    VERIFICATION_REVISION = "morganstanley.official-region-journey.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.fatal_reason: str | None = None
        self.failed_jobs: list[dict] = []
        self.failed_sources: list[dict] = []
        self.initial_counts: dict[str, int | None] = {}
        self.final_counts: dict[str, int | None] = {}
        self.initial_ids: dict[str, list[str]] = {}
        self.final_ids: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY, headers=HTML_HEADERS)
        entry_info = parse_official_entry(entry.text, str(entry.url))
        search = await self.http.get(SEARCH_PAGE_URL, headers=HTML_HEADERS)
        search_info = parse_search_page(search.text, str(search.url))
        metadata = await self.http.json(LOCATION_METADATA_URL, headers=HTML_HEADERS)
        routes = parse_location_metadata(metadata)
        for script_url in search_info["aggregate_scripts"]:
            script = await self.http.get(script_url, headers=HTML_HEADERS)
            text = script.text
            if "filter-metadata-location.json" not in text or "resultset.json" not in text:
                raise ValueError(
                    "Morgan Stanley opportunity aggregate script no longer references "
                    "the region metadata/resultset journey"
                )
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            requested_entry_url=OFFICIAL_ENTRY,
            official_entry=str(entry.url),
            official_entry_status=entry.status_code,
            official_entry_redirects=[str(item.url) for item in entry.history],
            official_entry_title=entry_info["title"],
            official_entry_og_url=entry_info["og_url"],
            official_entry_search_links=entry_info["search_links"],
            search_page=str(search.url),
            search_page_status=search.status_code,
            search_page_title=search_info["title"],
            location_metadata_status=metadata.status_code if hasattr(metadata, "status_code") else 200,
            aggregate_scripts=search_info["aggregate_scripts"],
            location_metadata_url=LOCATION_METADATA_URL,
            region_routes=routes,
            journey_steps=[
                OFFICIAL_ENTRY,
                SEARCH_PAGE_URL,
                LOCATION_METADATA_URL,
                EIGHTFOLD_BOARD_URL + "?source=mscom",
                CHINA_ORIGIN,
                routes["workday_urls"][0],
            ],
            listing_authority=(
                "Morgan Stanley's official region selector publishes the Eightfold board for "
                "Americas/EMEA/Hong Kong/India/Japan/All Others, the official China site for "
                "Mainland China and the company Workday External board filtered to Korea. "
                "All three are company-scoped; aggregators and the legacy AEM resultset "
                "(empty for opportunity=ep) are excluded."
            ),
            region_scope={
                "eightfold_regions": routes["eightfold_regions"],
                "china_regions": routes["china_regions"],
                "workday_regions": routes["workday_regions"],
            },
            date_behavior=(
                "Eightfold date_posted is the UTC date of postedTs; Workday date_posted is "
                "jobPostingInfo.startDate; China date_posted is the listing's M/D/YY "
                "unifiedStandardStart. Relative 'posted ago' labels and request time are never used."
            ),
            description_behavior=(
                "Eightfold stores position_details.jobDescription; China stores the job page "
                "[itemprop=description]; Workday stores jobPostingInfo.jobDescription. These "
                "are the full public description fields; site chrome, cookie text and "
                "similar-job cards are excluded."
            ),
        )
        return time.perf_counter() - started

    # ---------------------------------------------------------------- listing
    async def _eightfold_page(self, start: int, *, label: str):
        if not self.page_guard():
            raise ValueError("Morgan Stanley Eightfold pagination budget exhausted")
        response = await self.http.get(
            EIGHTFOLD_SEARCH_URL,
            params={
                "domain": EIGHTFOLD_DOMAIN,
                "query": "",
                "location": "",
                "start": start,
                "num": EIGHTFOLD_PAGE_SIZE,
                "sort_by": EIGHTFOLD_SORT,
            },
            headers=EIGHTFOLD_HEADERS,
        )
        page = parse_eightfold_search(response.json(), start)
        for issue in page["issues"]:
            self.result.warnings.append(f"{label}_{issue}")
        return page, str(response.url)

    async def _enumerate_eightfold(self, label: str) -> dict:
        started = time.perf_counter()
        rows_by_id: dict[str, dict] = {}
        ordered: list[str] = []
        pages: list[dict] = []
        issues: list[str] = []
        totals: list[int] = []
        start = 0
        while True:
            page, url = await self._eightfold_page(start, label=label)
            total = page["count"]
            totals.append(total)
            rows = page["rows"]
            if not rows:
                issues.append(f"{label}_empty_page_before_total:{start}")
                break
            current = [str(row["id"]) for row in rows]
            for row in rows:
                identity = str(row["id"])
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        issues.append(f"{label}_changed_row_for_id:{identity}")
                    continue
                rows_by_id[identity] = row
                ordered.append(identity)
            pages.append({
                "start": start,
                "rows": len(rows),
                "first_id": current[0] if current else None,
                "last_id": current[-1] if current else None,
                "source_total": total,
                "url": url,
            })
            if len(rows_by_id) >= total:
                break
            start += EIGHTFOLD_PAGE_SIZE
            if start > 20000:
                issues.append(f"{label}_page_budget_exhausted")
                break
        return {
            "rows_by_id": rows_by_id,
            "ordered_ids": ordered,
            "pages": pages,
            "issues": issues,
            "totals": totals,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _china_page(self, page_number: int, *, label: str):
        if not self.page_guard():
            raise ValueError("Morgan Stanley China pagination budget exhausted")
        response = await self.http.json(
            CHINA_JOBS_URL,
            method="POST",
            json={
                "locale": "en_US",
                "pageNumber": page_number,
                "sortBy": "",
                "keywords": "",
                "location": "",
                "facetFilters": {},
                "brand": "",
                "skills": [],
                "categoryId": 0,
                "alertId": "",
                "rcmCandidateId": "",
            },
            headers=CHINA_HEADERS,
        )
        page = parse_china_listing(response, page_number)
        for issue in page["issues"]:
            self.result.warnings.append(f"{label}_{issue}")
        return page

    async def _enumerate_china(self, label: str) -> dict:
        started = time.perf_counter()
        rows_by_id: dict[str, dict] = {}
        ordered: list[str] = []
        pages: list[dict] = []
        issues: list[str] = []
        totals: list[int] = []
        page_number = 0
        while True:
            page = await self._china_page(page_number, label=label)
            total = page["total"]
            totals.append(total)
            rows = page["rows"]
            if not rows:
                issues.append(f"{label}_empty_page_before_total:{page_number}")
                break
            current = [str(row["id"]) for row in rows]
            for row in rows:
                identity = str(row["id"])
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        issues.append(f"{label}_changed_row_for_id:{identity}")
                    continue
                rows_by_id[identity] = row
                ordered.append(identity)
            pages.append({
                "page": page_number,
                "rows": len(rows),
                "first_id": current[0] if current else None,
                "last_id": current[-1] if current else None,
                "source_total": total,
            })
            if len(rows_by_id) >= total:
                break
            page_number += 1
            if page_number > 1000:
                issues.append(f"{label}_page_budget_exhausted")
                break
        return {
            "rows_by_id": rows_by_id,
            "ordered_ids": ordered,
            "pages": pages,
            "issues": issues,
            "totals": totals,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _workday_page(self, offset: int, *, label: str):
        if not self.page_guard():
            raise ValueError("Morgan Stanley Workday pagination budget exhausted")
        response = await self.http.json(
            WORKDAY_JOBS_URL,
            method="POST",
            json={
                "appliedFacets": {"Location_Country": [WORKDAY_KOREA_COUNTRY_ID]},
                "limit": WORKDAY_PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            },
            headers=WORKDAY_HEADERS,
        )
        page = parse_workday_listing(response, offset)
        for issue in page["issues"]:
            self.result.warnings.append(f"{label}_{issue}")
        return page

    async def _enumerate_workday(self, label: str) -> dict:
        started = time.perf_counter()
        rows_by_path: dict[str, dict] = {}
        ordered: list[str] = []
        pages: list[dict] = []
        issues: list[str] = []
        totals: list[int] = []
        offset = 0
        while True:
            page = await self._workday_page(offset, label=label)
            total = page["total"]
            totals.append(total)
            rows = page["rows"]
            if not rows:
                issues.append(f"{label}_empty_page_before_total:{offset}")
                break
            for row in rows:
                identity = row["externalPath"]
                if identity in rows_by_path:
                    if rows_by_path[identity] != row:
                        issues.append(f"{label}_changed_row_for_id:{identity}")
                    continue
                rows_by_path[identity] = row
                ordered.append(identity)
            pages.append({
                "offset": offset,
                "rows": len(rows),
                "first_id": rows[0].get("bulletFields", [None])[0] if rows else None,
                "last_id": rows[-1].get("bulletFields", [None])[0] if rows else None,
                "source_total": total,
            })
            if len(rows_by_path) >= total:
                break
            offset += WORKDAY_PAGE_SIZE
            if offset > 10000:
                issues.append(f"{label}_page_budget_exhausted")
                break
        return {
            "rows_by_path": rows_by_path,
            "ordered_ids": ordered,
            "pages": pages,
            "issues": issues,
            "totals": totals,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    # ------------------------------------------------------------------- jobs
    def _job_eightfold(self, row: dict):
        identity = str(row["id"])
        return self.job(
            raw={
                "source": "eightfold",
                "source_listing_url": EIGHTFOLD_BOARD_URL + "?source=mscom",
                "listing": row,
            },
            title=_space(row.get("name")),
            url=EIGHTFOLD_ORIGIN + row["positionUrl"],
            job_id=identity,
            requisition_id=row.get("atsJobId") or row.get("displayJobId"),
            locations=list(row.get("locations") or []),
            department=row.get("department"),
            workplace_type=row.get("workLocationOption"),
            date_posted=epoch_date(row.get("postedTs")),
            date_posted_raw={
                "postedTs": row.get("postedTs"),
                "postedTs_iso": (
                    datetime.fromtimestamp(row["postedTs"], timezone.utc).isoformat()
                    if isinstance(row.get("postedTs"), (int, float)) else None
                ),
                "creationTs_not_used": row.get("creationTs"),
            },
        )

    def _job_china(self, row: dict):
        return self.job(
            raw={
                "source": "china",
                "source_listing_url": CHINA_SEARCH_URL,
                "listing": row,
            },
            title=_space(row.get("unifiedStandardTitle")),
            url=china_job_url(row),
            job_id=str(row["id"]),
            requisition_id=str(row["id"]),
            locations=list(row.get("mfield1") or []),
            date_posted=us_date(row.get("unifiedStandardStart")),
            valid_through=us_date(row.get("unifiedStandardEnd")),
            date_posted_raw={
                "unifiedStandardStart": row.get("unifiedStandardStart"),
                "unifiedStandardEnd": row.get("unifiedStandardEnd"),
                "source_format": "M/D/YY as published by careers.morganstanley.com.cn",
            },
        )

    def _job_workday(self, row: dict):
        requisition = str((row.get("bulletFields") or [""])[0])
        return self.job(
            raw={
                "source": "workday_korea",
                "source_listing_url": WORKDAY_JOBS_URL,
                "listing": row,
            },
            title=_space(row.get("title")),
            url=WORKDAY_ORIGIN + row["externalPath"],
            job_id=requisition,
            requisition_id=requisition,
            locations=[row.get("locationsText")] if row.get("locationsText") else [],
            date_posted_raw={"postedOn": row.get("postedOn")},
            warnings=[] if requisition else ["missing_requisition_id"],
        )

    # ---------------------------------------------------------------- details
    def _mark_fatal(self, reason: str):
        self.fatal_reason = reason
        self.result.errors.append(reason)
        self.failed_sources.append({"url": self.source.url, "reason": reason})

    async def _detail_eightfold(self, job):
        identity = str(job.job_id)
        if self.fatal_reason:
            job.warnings.append("skipped_after_fatal_source_response")
            job.description_unavailable = True
            return job
        detail_url = EIGHTFOLD_DETAIL_URL + "?" + urlencode({
            "position_id": identity,
            "domain": EIGHTFOLD_DOMAIN,
            "hl": "en",
        })
        try:
            response = await self.http.json(detail_url, headers=EIGHTFOLD_HEADERS)
        except Exception as exc:
            text = str(exc)
            if any(token in text for token in ("429", "403", "405", "challenge", "CAPTCHA")):
                self._mark_fatal(f"eightfold_fatal_source_response:{text}")
                job.warnings.append("fatal_source_response")
                job.description_unavailable = True
                return job
            if "404" in text or "410" in text:
                job.description_unavailable = True
                job.warnings.append("detail_http_404")
                job.description_method = "morganstanley.eightfold.position_details.jobDescription"
                job.description_source_url = EIGHTFOLD_DETAIL_URL
                self.failed_jobs.append({"id": identity, "url": job.url, "reason": "detail_http_404"})
                return job
            raise
        detail = parse_eightfold_detail(response, identity)
        return apply_eightfold_detail(job, detail, detail_url)

    async def _detail_china(self, job):
        identity = str(job.job_id)
        if self.fatal_reason:
            job.warnings.append("skipped_after_fatal_source_response")
            job.description_unavailable = True
            return job
        try:
            response = await self.http.get(job.url, headers={**HTML_HEADERS, **CHINA_HEADERS})
        except Exception as exc:
            text = str(exc)
            if any(token in text for token in ("429", "403", "405", "challenge", "CAPTCHA")):
                self._mark_fatal(f"china_fatal_source_response:{text}")
                job.warnings.append("fatal_source_response")
                job.description_unavailable = True
                return job
            if "404" in text or "410" in text:
                job.description_unavailable = True
                job.warnings.append("detail_http_404")
                job.description_method = "morganstanley.china_successfactors.itemprop_description"
                job.description_source_url = job.url
                self.failed_jobs.append({"id": identity, "url": job.url, "reason": "detail_http_404"})
                return job
            raise
        detail = parse_china_detail(response.text, job.raw["listing"], str(response.url))
        return apply_china_detail(job, detail, str(response.url))

    async def _detail_workday(self, job):
        requisition = str(job.job_id)
        if self.fatal_reason:
            job.warnings.append("skipped_after_fatal_source_response")
            job.description_unavailable = True
            return job
        path = job.raw["listing"]["externalPath"]
        detail_url = WORKDAY_DETAIL_PREFIX + path
        try:
            response = await self.http.json(detail_url, headers=WORKDAY_HEADERS)
        except Exception as exc:
            text = str(exc)
            if any(token in text for token in ("429", "403", "405", "challenge", "CAPTCHA")):
                self._mark_fatal(f"workday_fatal_source_response:{text}")
                job.warnings.append("fatal_source_response")
                job.description_unavailable = True
                return job
            if "404" in text or "410" in text:
                job.description_unavailable = True
                job.warnings.append("detail_http_404")
                job.description_method = "morganstanley.workday.jobPostingInfo.jobDescription"
                job.description_source_url = detail_url
                self.failed_jobs.append({"id": requisition, "url": job.url, "reason": "detail_http_404"})
                return job
            raise
        detail = parse_workday_detail(response, requisition)
        return apply_workday_detail(job, detail, detail_url)

    async def _detail(self, job):
        source = (job.raw or {}).get("source")
        if source == "eightfold":
            return await self._detail_eightfold(job)
        if source == "china":
            return await self._detail_china(job)
        if source == "workday_korea":
            return await self._detail_workday(job)
        raise ValueError(f"Morgan Stanley unknown job source: {source!r}")

    # ---------------------------------------------------------------- recheck
    @staticmethod
    def _row_for(source: dict, identity: str):
        rows = source.get("rows_by_id") or source.get("rows_by_path") or {}
        return rows.get(identity)

    def _source_summary(self, initial: dict, final: dict | None, *, unit: str) -> dict:
        initial_ids = set(initial["ordered_ids"])
        final_ids = set(final["ordered_ids"]) if final else set()
        added = sorted(final_ids - initial_ids)
        removed = sorted(initial_ids - final_ids)
        return {
            "advertised_total_initial": initial["totals"][0] if initial["totals"] else None,
            "unique_ids_initial": len(initial_ids),
            "pages_initial": len(initial["pages"]),
            "advertised_total_final": final["totals"][-1] if final and final["totals"] else None,
            "unique_ids_final": len(final_ids) if final else None,
            "pages_final": len(final["pages"]) if final else None,
            "added_during_run": added,
            "removed_during_run": removed,
            "changed_during_run": [
                identity for identity in sorted(initial_ids & final_ids)
                if self._row_for(initial, identity) != self._row_for(final, identity)
            ] if final else [],
            "issues": sorted(set(initial["issues"] + (final["issues"] if final else []))),
            "unit": unit,
            "page_rows": initial["pages"],
        }

    async def run(self):
        adapter_started = time.perf_counter()
        try:
            entry_seconds = await self._entry()
            listing_started = time.perf_counter()
            initial_eightfold = await self._enumerate_eightfold("eightfold")
            initial_china = await self._enumerate_china("china")
            initial_workday = await self._enumerate_workday("workday_korea")
            listing_seconds = max(0.0, time.perf_counter() - listing_started)
            self.result.metadata["entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.metadata["listing_phase_seconds"] = round(listing_seconds, 6)

            initial_counts = {
                "eightfold": initial_eightfold["totals"][0] if initial_eightfold["totals"] else None,
                "china": initial_china["totals"][0] if initial_china["totals"] else None,
                "workday_korea": initial_workday["totals"][0] if initial_workday["totals"] else None,
            }
            self.initial_counts = initial_counts
            self.initial_ids = {
                "eightfold": list(initial_eightfold["ordered_ids"]),
                "china": list(initial_china["ordered_ids"]),
                "workday_korea": list(initial_workday["ordered_ids"]),
            }
            for name, source in (
                ("eightfold", initial_eightfold),
                ("china", initial_china),
                ("workday_korea", initial_workday),
            ):
                for issue in source["issues"]:
                    self.result.errors.append(f"{name}_listing:{issue}")

            jobs = []
            for row in initial_eightfold["rows_by_id"].values():
                job = self._job_eightfold(row)
                jobs.append(job)
            for row in initial_china["rows_by_id"].values():
                jobs.append(self._job_china(row))
            for row in initial_workday["rows_by_path"].values():
                jobs.append(self._job_workday(row))
            for job in jobs:
                self.add(job)
            await self.hydrate(jobs, self._detail)

            # Final full listing recheck: detect additions/removals while details
            # were being hydrated. The union is the collected membership.
            final_started = time.perf_counter()
            final_eightfold = final_china = final_workday = None
            try:
                final_eightfold = await self._enumerate_eightfold("final_eightfold")
                final_china = await self._enumerate_china("final_china")
                final_workday = await self._enumerate_workday("final_workday")
            except Exception as exc:
                self.result.errors.append(f"final_listing_recheck_failed:{type(exc).__name__}:{exc}")
                self.failed_sources.append({"url": self.source.url, "reason": str(exc)})
            final_seconds = max(0.0, time.perf_counter() - final_started)
            self.result.metadata["final_listing_seconds"] = round(final_seconds, 6)

            if final_eightfold:
                self.final_counts["eightfold"] = (
                    final_eightfold["totals"][-1] if final_eightfold["totals"] else None
                )
                self.final_ids["eightfold"] = list(final_eightfold["ordered_ids"])
            if final_china:
                self.final_counts["china"] = final_china["totals"][-1] if final_china["totals"] else None
                self.final_ids["china"] = list(final_china["ordered_ids"])
            if final_workday:
                self.final_counts["workday_korea"] = (
                    final_workday["totals"][-1] if final_workday["totals"] else None
                )
                self.final_ids["workday_korea"] = list(final_workday["ordered_ids"])

            added_jobs = []
            if final_eightfold:
                known = {job.key for job in self.result.jobs}
                for identity in set(final_eightfold["ordered_ids"]) - set(initial_eightfold["ordered_ids"]):
                    job = self._job_eightfold(final_eightfold["rows_by_id"][identity])
                    if job.key not in known:
                        known.add(job.key)
                        self.add(job)
                        added_jobs.append(job)
            if final_china:
                known = {job.key for job in self.result.jobs}
                for identity in set(final_china["ordered_ids"]) - set(initial_china["ordered_ids"]):
                    job = self._job_china(final_china["rows_by_id"][identity])
                    if job.key not in known:
                        known.add(job.key)
                        self.add(job)
                        added_jobs.append(job)
            if final_workday:
                known = {job.key for job in self.result.jobs}
                for identity in set(final_workday["ordered_ids"]) - set(initial_workday["ordered_ids"]):
                    job = self._job_workday(final_workday["rows_by_path"][identity])
                    if job.key not in known:
                        known.add(job.key)
                        self.add(job)
                        added_jobs.append(job)
            if added_jobs:
                await self.hydrate(added_jobs, self._detail)

            summaries = {
                "eightfold": self._source_summary(initial_eightfold, final_eightfold, unit="position_id"),
                "china": self._source_summary(initial_china, final_china, unit="job_id"),
                "workday_korea": self._source_summary(initial_workday, final_workday, unit="externalPath"),
            }
            expected = (
                len(initial_eightfold["ordered_ids"])
                + len(initial_china["ordered_ids"])
                + len(initial_workday["ordered_ids"])
                + len(added_jobs)
            )
            self.result.expected_total = expected
            self.result.listing_complete = not self.result.errors and len(self.result.jobs) == expected

            cross_source = []
            seen_titles = {}
            for job in self.result.jobs:
                key = (_space(job.title).casefold(), tuple(_space(x) for x in job.locations))
                marker = (job.raw.get("source"), job.job_id)
                if key in seen_titles and seen_titles[key][0] != marker[0]:
                    cross_source.append({
                        "title": job.title,
                        "locations": job.locations,
                        "sources": [seen_titles[key][0], marker[0]],
                        "ids": [seen_titles[key][1], marker[1]],
                    })
                else:
                    seen_titles.setdefault(key, marker)
            self.result.metadata["membership"] = {
                "sources": summaries,
                "initial_counts": initial_counts,
                "final_counts": self.final_counts,
                "expected_total": expected,
                "jobs_collected": len(self.result.jobs),
                "added_during_run": len(added_jobs),
                "cross_source_duplicate_title_location": cross_source,
                "reconciliation": {
                    "eightfold_initial_unique": len(initial_eightfold["ordered_ids"]),
                    "eightfold_final_unique": (
                        len(final_eightfold["ordered_ids"]) if final_eightfold else None
                    ),
                    "china_initial_unique": len(initial_china["ordered_ids"]),
                    "china_final_unique": len(final_china["ordered_ids"]) if final_china else None,
                    "workday_korea_initial_unique": len(initial_workday["ordered_ids"]),
                    "workday_korea_final_unique": (
                        len(final_workday["ordered_ids"]) if final_workday else None
                    ),
                    "collected_union": len(self.result.jobs),
                    "expected_union": expected,
                },
            }
            self.result.metadata["failed_jobs"] = self.failed_jobs
            self.result.metadata["failed_sources"] = self.failed_sources
            self.result.metadata["overall_adapter_seconds"] = round(
                max(0.0, time.perf_counter() - adapter_started), 6
            )
            if self.failed_jobs or self.failed_sources or self.result.errors:
                self.result.listing_complete = False
            return self.result
        finally:
            self.result.finish()
