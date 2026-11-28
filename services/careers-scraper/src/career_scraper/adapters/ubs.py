"""UBS's own careers search page and the three SuccessFactors boards it publishes.

UBS's official "Search jobs" page (``OFFICIAL_ENTRY``) publishes exactly three
company job boards on ``jobs.ubs.com`` (SAP SuccessFactors Career Site Builder,
``partnerid=25008``):

* Professionals               — site ``5012``, published link query ``LinkID=15231``
* Students and graduates      — site ``5131``, published link query ``LinkID=15232``
* Swiss pupils                — site ``5054``, published link query ``LinkID=15233``

Membership is the union of those three published, unfiltered board queries.  The
``LinkID`` is part of the board definition: the same site without the published
link query exposes a smaller default search (observed 542/79/9 versus
597/87/28 on 2026-11-08), so the published links are required.

Each board page is server-rendered and embeds ``searchResultsResponse.JobsCount``
plus the first page in ``<input id="preLoadJSON">``.  Further rows come from the
board's own pagination request (``ProcessSortAndShowMoreJobs`` with
``SortType=LastUpdated``).  The full 2026-11-08 sweep reconciled exactly
597 + 87 + 28 = 712 distinct requisition ids with the three advertised counts.

Full role text comes from the exact server-rendered detail page for each listed
``reqid`` (``PageType=JobDetails``).  Its ``Jobdetails.JobDetailQuestions`` carry
the ordered, labelled description sections (``jobdescription``, the team,
skills/experience, about us, salary information, contact details, policy text …)
and all other public fields.  No aggregator, guessed slug or parent board is
used, and no ATS board is enumerated beyond the three links UBS publishes.

``lastupdated`` is rendered as an unlabelled date on the detail page, so it is
retained as ``date_updated``/raw only; ``date_posted`` stays null.  Apply is an
in-page Angular wizard with no public deep link, so ``apply_url`` is the exact
detail page that offers the apply button and ``raw.apply_route`` records that.
"""
from __future__ import annotations

from datetime import date, datetime
import html as html_module
import json
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.ubs.com/global/en/careers/search-jobs.html"
PARTNER_ID = "25008"
JOBS_ORIGIN = "https://jobs.ubs.com"
LISTING_PATH = "/tgnewui/search/home/homewithpreload"
SEARCH_API = JOBS_ORIGIN + "/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
LISTING_URL = (JOBS_ORIGIN + "/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008"
               "&siteid={site_id}&PageType=searchResults&SearchType=linkquery&LinkID={link_id}")
DETAIL_URL = (JOBS_ORIGIN + "/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008"
              "&siteid={site_id}&PageType=JobDetails&jobid={job_id}")
PAGE_SIZE = 50
DEFAULT_SORT = "LastUpdated"
FALLBACK_SORT = "JobTitle"

BOARDS = (
    {"key": "professionals", "label": "Professionals", "site_id": 5012, "link_id": 15231,
     "site_family": (5012, 5048, 5049, 5050)},
    {"key": "students_and_graduates", "label": "Students and graduates", "site_id": 5131,
     "link_id": 15232, "site_family": (5131, 5132, 5133)},
    {"key": "swiss_pupils", "label": "Swiss pupils", "site_id": 5054, "link_id": 15233,
     "site_family": (5054, 5055, 5056)},
)
BOARD_BY_KEY = {board["key"]: board for board in BOARDS}
LOCALE_NAMES = {"1033": "English", "1031": "German", "1036": "French", "1040": "Italian"}
MONTH_NUMBERS = {
    "jan": 1, "january": 1, "januar": 1, "janv": 1, "janvier": 1, "gen": 1, "gennaio": 1,
    "feb": 2, "february": 2, "februar": 2, "fev": 2, "fevr": 2, "fevrier": 2, "febbraio": 2,
    "mar": 3, "march": 3, "marz": 3, "maerz": 3, "mars": 3, "marzo": 3,
    "apr": 4, "april": 4, "avr": 4, "avril": 4, "aprile": 4,
    "may": 5, "mai": 5, "maggio": 5, "mag": 5,
    "jun": 6, "june": 6, "juni": 6, "juin": 6, "giu": 6, "giugno": 6,
    "jul": 7, "july": 7, "juli": 7, "juil": 7, "juillet": 7, "lug": 7, "luglio": 7,
    "aug": 8, "august": 8, "aout": 8, "ago": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "septembre": 9, "settembre": 9, "set": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10, "octobre": 10, "ottobre": 10,
    "ott": 10,
    "nov": 11, "november": 11, "novembre": 11,
    "dec": 12, "december": 12, "dez": 12, "dezember": 12, "decembre": 12, "dic": 12,
    "dicembre": 12,
}
DATE_TOKEN = re.compile(r"^(\d{1,2})[-/.\s]+([^\d\s]+)[-/.\s]+(\d{4})$")

# The UBS corporate content site answers the generic scraper user agent with an
# Akamai 403; the exact public page is served to a normal browser request.
ENTRY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
APPLY_ROUTE_NOTE = ("Detail page offers an in-page Angular 'Apply now' button "
                    "(#applyFromDetailBtn, postToNextPageFromDetails); no public "
                    "apply href or deep link is published.")
SECTION_CLASS = "jobDetailTextArea"
# UBS publishes multiple locations/cities as a comma-separated list.  One country
# name itself contains a comma ("Korea, Republic of"), so it is kept whole.
SINGLE_LOCATION_WITH_COMMA = {"korea, republic of"}


def normalized(value) -> str:
    """Collapse whitespace and decode the HTML entities UBS stores in answers."""
    return re.sub(r"\s+", " ", html_module.unescape(str(value or ""))).strip()


def _month_number(token: str):
    plain = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode()
    return MONTH_NUMBERS.get(plain.strip(".").lower())


def clean_text(html: str) -> str:
    """Readable text for the source HTML without BeautifulSoup's inline-tag gaps."""
    value = text_html(html or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"[ \t]+([,.;:!?])", r"\1", value)


def split_multi_value(value) -> list[str]:
    """Split UBS's comma-separated location/city answer without mangling names."""
    text = normalized(value)
    if not text:
        return []
    if text.lower() in SINGLE_LOCATION_WITH_COMMA:
        return [text]
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_source_date(value):
    """Parse UBS's rendered date, including its German/French/Italian month names.

    The board's own JSON always carries the English ``06-Nov-2026`` spelling, but
    a detail page in another language renders ``27-Okt-2026`` / ``01-Août-2026``.
    Anything that cannot be resolved to a real calendar date stays unknown.
    """
    value = normalized(value)
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    match = DATE_TOKEN.match(value)
    if match:
        day, token, year = match.groups()
        month = _month_number(token)
        if month:
            try:
                return date(int(year), month, int(day)).isoformat()
            except ValueError:
                return None
    return None


def extract_preload(html: str) -> dict:
    """Return the server-embedded page model (``input#preLoadJSON``)."""
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.find("input", id="preLoadJSON")
    if node is None or not node.get("value"):
        raise ValueError("UBS page has no input#preLoadJSON server model")
    try:
        data = json.loads(node["value"])
    except json.JSONDecodeError as exc:
        raise ValueError("UBS input#preLoadJSON is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("UBS input#preLoadJSON is not an object")
    return data


def _query(query: dict, key: str):
    values = query.get(key)
    return values[0] if values else None


def _assert_jobs_url(url: str, *, page_type: str | None = None):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "jobs.ubs.com":
        raise ValueError(f"UBS request left jobs.ubs.com: {url}")
    if parts.path.lower() != LISTING_PATH:
        raise ValueError(f"UBS request left its Career Site Builder route: {url}")
    query = parse_qs(parts.query)
    if _query(query, "partnerid") != PARTNER_ID:
        raise ValueError(f"UBS request is not partnerid={PARTNER_ID}: {url}")
    if page_type and (_query(query, "PageType") or "").lower() != page_type.lower():
        raise ValueError(f"UBS request is not PageType={page_type}: {url}")
    return query


def _assert_board_url(url: str, board: dict):
    query = _assert_jobs_url(url, page_type="searchResults")
    if _query(query, "siteid") != str(board["site_id"]):
        raise ValueError(f"UBS board {board['key']} opened siteid={_query(query, 'siteid')}: {url}")
    if _query(query, "SearchType") != "linkquery":
        raise ValueError(f"UBS board {board['key']} is not the published link query: {url}")
    if _query(query, "LinkID") != str(board["link_id"]):
        raise ValueError(f"UBS board {board['key']} LinkID changed: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse the three job-board links published by UBS's own Search jobs page."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.ubs.com"
            or parts.path.rstrip("/") != "/global/en/careers/search-jobs.html"):
        raise ValueError(f"UBS official entry redirected away from its inspected URL: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Search jobs" not in title:
        raise ValueError(f"UBS official entry title changed: {title!r}")

    found: dict[str, dict] = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor.get("href", "").strip())
        target = urlsplit(href)
        if target.scheme != "https" or target.netloc != "jobs.ubs.com":
            continue
        query = parse_qs(target.query)
        if _query(query, "partnerid") != PARTNER_ID:
            continue
        if _query(query, "PageType") != "searchResults" or _query(query, "SearchType") != "linkquery":
            continue
        site_id, link_id = _query(query, "siteid"), _query(query, "LinkID")
        for board in BOARDS:
            if site_id == str(board["site_id"]) and link_id == str(board["link_id"]):
                found.setdefault(board["key"], {
                    "key": board["key"],
                    "label": normalized(anchor.get_text(" ", strip=True)),
                    "url": href,
                    "site_id": board["site_id"],
                    "link_id": board["link_id"],
                })
    missing = [board["key"] for board in BOARDS if board["key"] not in found]
    if missing:
        raise ValueError(f"UBS official entry no longer publishes these board links: {missing}")
    for key in found:
        _assert_board_url(found[key]["url"], BOARD_BY_KEY[key])
    return {
        "url": response_url,
        "title": title,
        "boards": found,
        "board_order": [board["key"] for board in BOARDS],
    }


def _listing_questions(row: dict) -> dict:
    questions = {}
    for question in row.get("Questions") or []:
        name = question.get("QuestionName")
        if name and name not in questions:
            questions[name] = question.get("Value")
    return questions


def parse_listing_row(row: dict, board: dict) -> dict:
    """Normalise one board row (article id + the listing questions UBS publishes)."""
    values = _listing_questions(row)
    job_id = normalized(values.get("reqid"))
    title = normalized(values.get("jobtitle"))
    site_id = normalized(values.get("siteid"))
    if not job_id or not title:
        raise ValueError(f"UBS {board['key']} row is missing its requisition id or title")
    if not site_id.isdigit():
        raise ValueError(f"UBS {board['key']} row {job_id} has no numeric siteid")
    if int(site_id) not in board["site_family"]:
        raise ValueError(f"UBS {board['key']} row {job_id} has unexpected siteid {site_id}")
    return {
        "job_id": job_id,
        "site_id": int(site_id),
        "title": title,
        "location_raw": normalized(values.get("formtext23")),
        "function_category": normalized(values.get("formtext21")),
        "business_division": normalized(values.get("department")),
        "lastupdated_raw": values.get("lastupdated"),
        "lastupdated_iso": parse_source_date(values.get("lastupdated")),
        "listing_link": row.get("Link"),
        "description_preview": normalized(values.get("jobdescription")),
        "source_row": row,
    }


def parse_board_listing(html: str, *, board: dict, response_url: str) -> dict:
    """Parse the published board page: advertised count, first page and search model."""
    _assert_board_url(response_url, board)
    data = extract_preload(html)
    model = data.get("SmartSearchJSONValue")
    if isinstance(model, str):
        model = json.loads(model)
    if not isinstance(model, dict):
        raise ValueError(f"UBS board {board['key']} page has no SmartSearchJSONValue")
    if str(model.get("SiteId")) != str(board["site_id"]):
        raise ValueError(f"UBS board {board['key']} preload SiteId changed: {model.get('SiteId')}")
    if str(model.get("PartnerId")) != PARTNER_ID:
        raise ValueError(f"UBS board {board['key']} preload PartnerId changed")
    session = model.get("EncryptedSessionValue")
    if not isinstance(session, str) or not session:
        raise ValueError(f"UBS board {board['key']} page has no encrypted search session")
    results = data.get("searchResultsResponse")
    if not isinstance(results, dict):
        raise ValueError(f"UBS board {board['key']} page has no searchResultsResponse")
    count = results.get("JobsCount")
    if not isinstance(count, int) or count < 0:
        raise ValueError(f"UBS board {board['key']} JobsCount is not a public count")
    jobs = (results.get("Jobs") or {}).get("Job") if isinstance(results.get("Jobs"), dict) else None
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise ValueError(f"UBS board {board['key']} Jobs.Job is not a list")
    if len(jobs) > PAGE_SIZE:
        raise ValueError(f"UBS board {board['key']} first page exceeds {PAGE_SIZE} rows")
    rows = [parse_listing_row(row, board) for row in jobs]
    return {
        "count": count,
        "rows": rows,
        "model": model,
        "page_size": len(rows),
        "keyword_fields": model.get("KeywordCustomSolrFields"),
        "location_fields": model.get("LocationCustomSolrFields"),
    }


def search_payload(model: dict, *, board: dict, sort: str, page_number: int) -> dict:
    """Exact body of the board's own pagination request (observed in search.min.js)."""
    return {
        "partnerId": PARTNER_ID,
        "siteId": str(board["site_id"]),
        "keyword": "",
        "location": "",
        "keywordCustomSolrFields": model.get("KeywordCustomSolrFields"),
        "locationCustomSolrFields": model.get("LocationCustomSolrFields"),
        "facetfilterfields": {"Facet": []},
        "powersearchoptions": {"PowerSearchOption": []},
        "SortType": sort,
        "pageNumber": page_number,
        "encryptedSessionValue": model.get("EncryptedSessionValue"),
        "linkId": "",
    }


def parse_search_page(value, *, board: dict, requested_page: int, total: int,
                      page_size: int = PAGE_SIZE) -> dict:
    """Validate one pagination response against the advertised board total."""
    if not isinstance(value, dict):
        raise ValueError(f"UBS {board['key']} search page {requested_page} is not an object")
    count = value.get("JobsCount")
    if count != total:
        raise ValueError(
            f"UBS {board['key']} JobsCount changed during the sweep: {count} != {total}")
    jobs = (value.get("Jobs") or {}).get("Job") if isinstance(value.get("Jobs"), dict) else None
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise ValueError(f"UBS {board['key']} search page {requested_page} Jobs.Job is not a list")
    pages = math.ceil(total / page_size) if total else 0
    if not 1 <= requested_page <= pages:
        raise ValueError(f"UBS {board['key']} requested page {requested_page} outside 1..{pages}")
    expected = page_size if requested_page < pages else total - page_size * (pages - 1)
    if len(jobs) != expected:
        raise ValueError(
            f"UBS {board['key']} page {requested_page} returned {len(jobs)} rows, expected {expected}")
    return {
        "page": requested_page,
        "count": count,
        "rows": [parse_listing_row(row, board) for row in jobs],
    }


def parse_salary(value) -> dict | None:
    raw = normalized(value)
    if not raw:
        return None
    ranges = []
    pattern = (r"min\s+(?:([A-Z]{3})\s+)?([\d,]+)\s*-\s*max\s+"
               r"(?:([A-Z]{3})\s+)?([\d,]+)")
    for low_currency, low, high_currency, high in re.findall(pattern, raw):
        ranges.append({"currency": low_currency or high_currency or None,
                       "min": int(low.replace(",", "")),
                       "max": int(high.replace(",", ""))})
    period = "annum" if re.search(r"/\s*annum|per annum", raw, re.I) else None
    return {"raw": raw, "ranges": ranges, "period": period}


def parse_job_detail(html: str, *, expected_job_id: str, response_url: str = "",
                     board: dict | None = None) -> dict:
    """Parse one exact UBS detail page into all published fields and sections."""
    data = extract_preload(html)
    details = data.get("Jobdetails")
    if not isinstance(details, dict):
        raise ValueError(
            f"UBS detail page for {expected_job_id} has no Jobdetails (expired or changed posting)")
    questions = details.get("JobDetailQuestions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"UBS detail page for {expected_job_id} has no job detail fields")
    fields: dict[str, dict] = {}
    for question in questions:
        zone = question.get("VerityZone")
        if zone and zone not in fields:
            fields[zone] = question

    def answer(zone: str):
        question = fields.get(zone)
        return None if question is None else question.get("AnswerValue")

    job_id = normalized(answer("reqid") or data.get("JobId"))
    if job_id != str(expected_job_id):
        raise ValueError(f"UBS detail page identity changed: {job_id} != {expected_job_id}")
    title = normalized(details.get("Title") or answer("jobtitle"))
    if not title:
        raise ValueError(f"UBS detail page {job_id} has no title")

    sections = []
    for question in questions:
        class_name = question.get("ClassName") or ""
        if SECTION_CLASS not in class_name:
            continue
        value = question.get("AnswerValue")
        if value is None or not str(value).strip():
            continue
        section_html = html_module.unescape(str(value))
        section_text = text_html(section_html)
        if not section_text.strip():
            continue
        sections.append({
            "field": question.get("VerityZone"),
            "heading": normalized(question.get("QuestionName")) or None,
            "html": section_html,
            "text": section_text,
            "answer_raw": value,
        })
    if not sections:
        raise ValueError(f"UBS detail page {job_id} publishes no job description sections")
    html_parts = []
    for section in sections:
        if section["heading"]:
            html_parts.append(f"<h3>{html_module.escape(section['heading'])}</h3>")
        html_parts.append(section["html"])
    description_html = "\n".join(html_parts)

    location_raw = normalized(answer("formtext23"))
    city_raw = normalized(answer("formtext2"))
    locations = split_multi_value(location_raw)
    if not locations and city_raw:
        locations = split_multi_value(city_raw)
    contact = text_html(html_module.unescape(str(answer("formtext43") or "")))
    employer = contact.splitlines()[0].strip() if contact.strip() else None
    salary = parse_salary(answer("formtext94"))
    locale_id = str(details.get("localeId") or "")
    language = None
    for item in data.get("TGLocales") or []:
        if str(item.get("LocaleId")) == locale_id and item.get("LocaleName"):
            language = normalized(item["LocaleName"])
            break
    if language is None:
        language = LOCALE_NAMES.get(locale_id)
    lastupdated = answer("lastupdated")
    program_start = answer("formdate1") or answer("formdate2")
    section_by_field = {section["field"]: section for section in sections}
    return {
        "job_id": job_id,
        "title": title,
        "requisition_id": normalized(answer("autoreq")) or None,
        "locations": locations,
        "location_raw": location_raw,
        "city": city_raw or None,
        "cities": split_multi_value(city_raw),
        "function_category": normalized(answer("formtext21")) or None,
        "business_division": normalized(answer("department")) or None,
        "employment_type": normalized(answer("formtext22")) or None,
        "employer": employer,
        "program_type": normalized(answer("formtext27")) or None,
        "program_start_raw": program_start,
        "program_start_iso": parse_source_date(program_start),
        "lastupdated_raw": lastupdated,
        "date_updated": parse_source_date(lastupdated),
        "date_posted": None,
        "salary": salary,
        "qualifications": (section_by_field.get("formtext59") or {}).get("text") or None,
        "responsibilities": (section_by_field.get("jobdescription") or {}).get("text") or None,
        "description_html": description_html,
        "sections": sections,
        "language": language,
        "locale_id": locale_id or None,
        "is_evergreen": bool(details.get("isEverGreen")),
        "job_site_id": normalized(details.get("JobSiteId")) or None,
        "auto_requisition": details.get("AutoOptionalReqId"),
        "detail_link": details.get("Link"),
        "fields": [{"verity_zone": question.get("VerityZone"),
                    "question_name": question.get("QuestionName"),
                    "class_name": question.get("ClassName"),
                    "answer_raw": question.get("AnswerValue")}
                   for question in questions],
        "board": board["key"] if board else None,
        "response_url": response_url,
    }


class UBS(Adapter):
    """Company-scoped UBS adapter over its three published SuccessFactors boards."""

    async def _entry(self):
        try:
            response = await self.http.request("GET", OFFICIAL_ENTRY, headers=ENTRY_HEADERS)
            parsed = parse_official_entry(response.text, str(response.url))
            self.result.metadata["official_entry"] = {
                "url": OFFICIAL_ENTRY,
                "status": response.status_code,
                "final_url": str(response.url),
                "title": parsed["title"],
                "boards": {key: {"label": value["label"], "url": value["url"]}
                           for key, value in parsed["boards"].items()},
            }
            return parsed
        except Exception as exc:
            self.result.errors.append(f"UBS official entry: {type(exc).__name__}: {exc}")
            self.result.metadata["official_entry"] = {
                "url": OFFICIAL_ENTRY,
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "boards": {board["key"]: LISTING_URL.format(site_id=board["site_id"],
                                                            link_id=board["link_id"])
                           for board in BOARDS},
                "fallback_basis": ("Published board links observed on the inspected UBS Search jobs "
                                  "page; used because the entry request failed during this run."),
            }
            return None

    async def _sweep(self, board: dict, *, total: int, model: dict, sort: str) -> dict:
        rows: dict[str, dict] = {}
        pages = math.ceil(total / PAGE_SIZE) if total else 0
        for page_number in range(1, pages + 1):
            body = search_payload(model, board=board, sort=sort, page_number=page_number)
            value = await self.http.json(SEARCH_API, method="POST", json=body)
            parsed = parse_search_page(value, board=board, requested_page=page_number,
                                       total=total)
            for row in parsed["rows"]:
                rows.setdefault(row["job_id"], row)
        return rows

    async def _board(self, board: dict) -> dict:
        started = time.perf_counter()
        url = LISTING_URL.format(site_id=board["site_id"], link_id=board["link_id"])
        response = await self.http.get(url)
        _assert_board_url(str(response.url), board)
        listing = parse_board_listing(response.text, board=board, response_url=str(response.url))
        total = listing["count"]
        model = listing["model"]
        rows = await self._sweep(board, total=total, model=model, sort=DEFAULT_SORT)
        primary_order = list(rows.values())
        fallback = None
        if len(rows) != total:
            fallback = await self._sweep(board, total=total, model=model, sort=FALLBACK_SORT)
            before = len(rows)
            for job_id, row in fallback.items():
                rows.setdefault(job_id, row)
            self.result.warnings.append(
                f"ubs_{board['key']}_sort_tie_repair: {before} of {total} rows under "
                f"{DEFAULT_SORT}; {len(rows)} unique after {FALLBACK_SORT} union")
        if len(rows) != total:
            self.result.errors.append(
                f"ubs_{board['key']}_membership_unclosed: {len(rows)} unique ids for "
                f"advertised {total}")
        swept = list(rows.values())
        first_page_ids = [row["job_id"] for row in primary_order[:PAGE_SIZE]]
        swept_first = [row["job_id"] for row in listing["rows"]]
        metadata = {
            "label": board["label"],
            "url": url,
            "site_id": board["site_id"],
            "link_id": board["link_id"],
            "advertised_count": total,
            "pages": math.ceil(total / PAGE_SIZE) if total else 0,
            "page_size": PAGE_SIZE,
            "unique_ids": len(rows),
            "first_page_ids": first_page_ids,
            "first_page_matches_preload": sorted(first_page_ids) == sorted(swept_first),
            "last_page_ids": [row["job_id"] for row in primary_order[-5:]],
            "fallback_added_ids": len(swept) - len(primary_order),
            "site_ids": sorted({row["site_id"] for row in rows.values()}),
            "sorts_used": [DEFAULT_SORT] + ([FALLBACK_SORT] if fallback is not None else []),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }
        return {"board": board, "rows": rows, "metadata": metadata}

    async def run(self):
        started = time.perf_counter()
        metadata = self.result.metadata
        metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_authority": (
                "UBS's own Search jobs page publishes exactly three company boards on its "
                "SuccessFactors site (partnerid=25008): Professionals (5012/LinkID 15231), "
                "Students and graduates (5131/LinkID 15232) and Swiss pupils (5054/LinkID "
                "15233). Membership is the union of those unfiltered published board queries."),
            "detail_method": (
                "Exact server-rendered PageType=JobDetails response for every listed reqid; "
                "ordered Jobdetails.JobDetailQuestions with class jobDetailTextArea provide "
                "the full labelled description sections."),
            "date_semantics": (
                "The detail page renders 'lastupdated' as an unlabelled top date; it is kept "
                "as date_updated plus raw lastupdated. date_posted/valid_through stay null; "
                "programme start dates (formdate1) stay raw and are not repurposed."),
            "apply_route": APPLY_ROUTE_NOTE,
            "membership_sources": {board["key"]: LISTING_URL.format(
                site_id=board["site_id"], link_id=board["link_id"]) for board in BOARDS},
        })

        collection_started = time.perf_counter()
        entry = await self._entry()
        boards = {}
        failed_boards = []
        for board in BOARDS:
            try:
                boards[board["key"]] = await self._board(board)
            except Exception as exc:
                failed_boards.append({"board": board["key"], "url": LISTING_URL.format(
                    site_id=board["site_id"], link_id=board["link_id"]),
                    "reason": f"{type(exc).__name__}: {exc}"})
                self.result.errors.append(f"UBS {board['key']} board: {type(exc).__name__}: {exc}")
        metadata["failed_boards"] = failed_boards
        metadata["board_collection_seconds"] = round(
            max(0.0, time.perf_counter() - collection_started), 6)
        metadata["boards"] = {key: value["metadata"] for key, value in boards.items()}

        all_rows: dict[str, dict] = {}
        cross_duplicates = []
        for board in BOARDS:
            result = boards.get(board["key"])
            if not result:
                continue
            for job_id, row in result["rows"].items():
                if job_id in all_rows:
                    cross_duplicates.append({"job_id": job_id,
                                             "boards": [all_rows[job_id]["board"], board["key"]]})
                    continue
                all_rows[job_id] = {**row, "board": board["key"]}
        metadata["cross_board_duplicates"] = cross_duplicates
        metadata["union_count"] = len(all_rows)
        self.result.pages = sum(value["metadata"]["pages"] for value in boards.values())
        self.result.timings["source_collection_seconds"] = metadata["board_collection_seconds"]

        if not all_rows:
            self.result.listing_complete = False
            self.result.errors.append("ubs_no_listing_rows_collected")
            return self.result

        sampled = False
        for row in all_rows.values():
            if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
                sampled = True
                break
            board = BOARD_BY_KEY[row["board"]]
            stub = self.job(
                {
                    "board": row["board"],
                    "membership_source": "ubs_published_board_linkquery",
                    "site_id": row["site_id"],
                    "detail_site_id": board["site_id"],
                    "listing_link": row["listing_link"],
                    "listing_row": row["source_row"],
                    "lastupdated_raw": row["lastupdated_raw"],
                },
                title=row["title"],
                # Canonical company detail route: the job's own row may be served
                # under any language site id of the same board, so pin the board's
                # primary site for a stable identity and URL across runs.
                url=DETAIL_URL.format(site_id=board["site_id"], job_id=row["job_id"]),
                job_id=row["job_id"],
                locations=[row["location_raw"]] if row["location_raw"] else [],
                department=row["business_division"] or None,
                team=row["function_category"] or None,
                date_updated=row["lastupdated_iso"],
            )
            self.add(stub)
        self.result.expected_total = len(all_rows)
        metadata["sampled"] = sampled
        if sampled:
            self.result.warnings.append("sample_limit")
            self.result.listing_complete = False

        async def detail(job):
            board = BOARD_BY_KEY[job.raw["board"]]
            response = await self.http.get(job.url)
            query = _assert_jobs_url(str(response.url), page_type="JobDetails")
            if _query(query, "jobid") != str(job.job_id):
                raise ValueError(f"UBS detail route changed job id: {response.url}")
            parsed = parse_job_detail(response.text, expected_job_id=job.job_id,
                                      response_url=str(response.url), board=board)
            date_updated = parsed["date_updated"]
            date_updated_source = "detail lastupdated"
            if date_updated is None:
                # The detail page may render a localized month the parser cannot
                # resolve; the board row carries the same date in English.
                date_updated = job.raw.get("lastupdated_iso")
                date_updated_source = "board row lastupdated (detail value not parseable)"
            salary = parsed["salary"]
            built = self.job(
                {
                    "board": job.raw["board"],
                    "membership_source": "ubs_published_board_linkquery",
                    "site_id": job.raw["site_id"],
                    "detail_site_id": job.raw.get("detail_site_id"),
                    "listing_link": job.raw["listing_link"],
                    "listing_row": job.raw["listing_row"],
                    "requisition_raw": parsed["requisition_id"],
                    "auto_requisition": parsed["auto_requisition"],
                    "lastupdated_raw": parsed["lastupdated_raw"],
                    "date_updated_source": date_updated_source,
                    "location_raw": parsed["location_raw"],
                    "city_raw": parsed["city"],
                    "cities": parsed["cities"],
                    "program_type": parsed["program_type"],
                    "program_start_raw": parsed["program_start_raw"],
                    "is_evergreen": parsed["is_evergreen"],
                    "job_site_id": parsed["job_site_id"],
                    "locale_id": parsed["locale_id"],
                    "detail_link": parsed["detail_link"],
                    "fields": parsed["fields"],
                    "sections": [{"field": section["field"],
                                  "heading": section["heading"],
                                  "text": section["text"]} for section in parsed["sections"]],
                    "apply_route": APPLY_ROUTE_NOTE,
                },
                title=parsed["title"],
                url=job.url,
                job_id=parsed["job_id"],
                requisition_id=parsed["requisition_id"],
                apply_url=job.url,
                description=parsed["description_html"],
                description_text=clean_text(parsed["description_html"]),
                method="successfactors_jobdetails_jobdetailtextareas",
                description_url=str(response.url),
                locations=parsed["locations"],
                department=parsed["business_division"] or None,
                team=parsed["function_category"] or None,
                employment_type=parsed["employment_type"],
                date_posted=None,
                date_posted_raw=None,
                date_updated=date_updated,
                salary=salary,
                qualifications=parsed["qualifications"],
                responsibilities=parsed["responsibilities"],
                employer=parsed["employer"],
                language=parsed["language"],
            )
            return built

        await self.hydrate(self.result.jobs, detail)

        # Final reconciliation: board membership can move while details are fetched.
        recheck_started = time.perf_counter()
        final_counts = {}
        changed = {}
        for board in BOARDS:
            if not boards.get(board["key"]):
                continue
            url = LISTING_URL.format(site_id=board["site_id"], link_id=board["link_id"])
            try:
                response = await self.http.get(url)
                parsed = parse_board_listing(response.text, board=board,
                                             response_url=str(response.url))
                final_counts[board["key"]] = parsed["count"]
                if parsed["count"] != boards[board["key"]]["metadata"]["advertised_count"]:
                    changed[board["key"]] = {
                        "start": boards[board["key"]]["metadata"]["advertised_count"],
                        "finish": parsed["count"],
                    }
            except Exception as exc:
                self.result.errors.append(
                    f"UBS final {board['key']} recheck: {type(exc).__name__}: {exc}")
        self.result.timings["final_recheck_seconds"] = round(
            max(0.0, time.perf_counter() - recheck_started), 6)
        metadata["counts_at_finish"] = final_counts
        metadata["count_changes_during_run"] = changed
        if changed:
            self.result.warnings.append(
                f"ubs_listing_changed_during_run: {sorted(changed)}")
            self.result.listing_complete = False
        membership_closed = (
            not sampled
            and not failed_boards
            and not changed
            and not cross_duplicates
            and all(value["metadata"]["unique_ids"] == value["metadata"]["advertised_count"]
                    for value in boards.values())
            and len(all_rows) == sum(value["metadata"]["advertised_count"]
                                     for value in boards.values())
        )
        self.result.listing_complete = bool(membership_closed)
        if not sampled and len(self.result.jobs) != len(all_rows):
            self.result.errors.append(
                f"ubs_exported_membership_mismatch: {len(self.result.jobs)} vs {len(all_rows)}")
            self.result.listing_complete = False
        metadata["membership_closed"] = self.result.listing_complete
        metadata["run_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        self.result.timings["total_run_seconds"] = metadata["run_seconds"]
        if entry is None:
            self.result.listing_complete = False
        return self.result
