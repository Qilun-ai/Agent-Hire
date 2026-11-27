"""Tech Mahindra's own careers portal: a server-rendered ASP.NET WebForms journey.

The official entry ``https://careers.techmahindra.com/`` publishes two
first-party opportunity routes: ``CurrentOpportunity.aspx`` ("IT
Opportunities") and ``BpsOpportunity.aspx`` ("BPS Opportunities").  The IT page
shows a 12-row "Latest IT Jobs" DataList with a pager whose pages overlap; its
unfiltered free-text search is the complete public membership and returned 490
unique posting codes at review time.  The BPS page publishes no current rows and
its empty search currently redirects to the site's own 500 error page.

Each listing card carries the published ``HdnJobCode`` and an
``Apply/Shortlist`` postback.  That postback selects the posting in the
server-side session; ``JobDetails.aspx`` then renders the selected posting's
full public fields: title, degree note, Skill Set, Total Experience, openings,
Job Post Date, Job Expiry Date, Domain, Location, Job Reference No and the
complete ``Job Summary`` body.  The site publishes no stable per-posting detail
URL, so the official detail route is recorded once and the published listing
code/reference number identify each record.  The flow submits no account,
application or CAPTCHA.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.techmahindra.com/"
LISTING_URL = "https://careers.techmahindra.com/CurrentOpportunity.aspx"
BPS_URL = "https://careers.techmahindra.com/BpsOpportunity.aspx"
DETAIL_URL = "https://careers.techmahindra.com/JobDetails.aspx"
HOST = "careers.techmahindra.com"

CARD_CLASS = "paragraph--type--card-info-stand-tiles"
FREE_SEARCH_BUTTON = "ctl00$ContentPlaceHolder1$btnFreeSearch"
FREE_SEARCH_INPUT = "ctl00$ContentPlaceHolder1$txtAdvanceSearch"
BPS_SEARCH_BUTTON = "ctl00$ContentPlaceHolder1$btnSearchJobs"
DETAIL_TITLE_ID = "ctl00_ContentPlaceHolder1_lblDesignationName"
DETAIL_SECTION_ID = "ctl00_ContentPlaceHolder1_JDSetion"
DETAIL_DEGREE_ID = "ctl00_ContentPlaceHolder1_dvDegree"
JOB_CODE = re.compile(r"^\d{4,8}$")
DATE_VALUE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
LOCATION_VALUE = re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")
BLOCK_MARKERS = (
    "this transaction/access is blocked due to security reasons",
    "access denied",
    "request rejected",
    "verify you are human",
    "captcha challenge",
)
ERROR_PAGE_MARKERS = (
    "unexpected error occurs, please try after sometime",
    "unexpected error occurs",
    "object moved",
)
COUNTRY_BLOCK_MARKER = "this job does not belong to your country"
METADATA_LABELS = (
    "Skill Set", "Total Experience", "No of Openings", "Job Post Date",
    "Job Expiry Date", "Domain", "Location", "Job Reference No",
)
SECTION_RESPONSIBILITY = re.compile(
    r"responsibilit|role description|roles and responsibilities|^jd\b|capabilit|duties", re.I
)
SECTION_QUALIFICATION = re.compile(
    r"qualification|education|skills|experience|mandatory skills|preferred skills|key skills|"
    r"must-have|must have|nice to have|who you are|requirements", re.I
)


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def normalized_body_text(html: str | None) -> str:
    return normalized(text_html(html or ""))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def check_origin(url: str, path: str | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise ValueError(f"Tech Mahindra response came from an unexpected origin: {url}")
    if path is not None and parts.path != path:
        raise ValueError(f"Tech Mahindra response came from an unexpected path: {url}")


def blocked(html: str, status: int | None = None) -> bool:
    if status in (401, 403, 429):
        return True
    lower = (html or "").lower()
    return any(marker in lower for marker in BLOCK_MARKERS)


def error_page(html: str, final_url: str = "", status: int | None = None) -> bool:
    path = urlsplit(final_url).path.lower() if final_url else ""
    if path.endswith(("/404.aspx", "/500.aspx")):
        return True
    if status is not None and status >= 500:
        return True
    lower = (html or "").lower()
    return any(marker in lower for marker in ERROR_PAGE_MARKERS)


class SourceBlocked(RuntimeError):
    """The official source returned a WAF/challenge/rate-limit response."""


class DetailUnavailable(RuntimeError):
    """The official detail route returned a site error instead of a posting."""


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    check_origin(response_url, "/")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "careers" not in title.lower() or "tech mahindra" not in title.lower():
        raise ValueError("Tech Mahindra official careers entry title changed")
    links = {a.get("href") for a in soup.select("a[href]")}
    for route in ("CurrentOpportunity.aspx", "BpsOpportunity.aspx", "Signin.aspx"):
        if route not in links:
            raise ValueError(f"Tech Mahindra official entry no longer links {route}")
    body = normalized_body_text(html)
    if "Tech Mahindra Limited" not in body:
        raise ValueError("Tech Mahindra official entry no longer declares its employer")
    return {
        "url": response_url,
        "title": title,
        "listing_route": "CurrentOpportunity.aspx",
        "bps_route": "BpsOpportunity.aspx",
        "signin_route": "Signin.aspx",
        "employer": "Tech Mahindra Limited",
    }


def _card_fields(card: Tag) -> dict:
    code_node = card.select_one('input[id$="HdnJobCode"]')
    button = card.select_one('input[id$="btn_jobs"]')
    category_node = card.select_one("div.title2 span, div[class*='title2'] span")
    title_node = card.select_one('div[style*="margin-bottom: 5px"]')
    paragraph = card.select_one("p")
    code = normalized(code_node.get("value")) if code_node else ""
    title = normalized(title_node.get_text(" ", strip=True)) if title_node else ""
    skill = experience = location = None
    if paragraph is not None:
        text = paragraph.get_text("\n", strip=True)
        match = re.search(r"Skill Set\s*:\s*(.*?)(?:\n|$)", text, re.I)
        if match:
            skill = normalized(match.group(1))
        match = re.search(r"Experience\s*:\s*(.*?)(?:\n|$)", text, re.I)
        if match:
            experience = normalized(match.group(1))
        match = re.search(r"Location\s*:\s*(.*?)(?:\n|$)", text, re.I)
        if match:
            location = normalized(match.group(1))
    return {
        "code": code,
        "category": normalized(category_node.get_text(" ", strip=True)) if category_node else None,
        "title": title,
        "listing_skill_text": skill,
        "listing_experience_text": experience,
        "listing_location_text": location,
        "button_name": button.get("name") if button else None,
        "button_value": button.get("value") if button else None,
        "code_input_name": code_node.get("name") if code_node else None,
    }


def parse_listing(html: str, response_url: str = LISTING_URL, *, expected_path: str | None = None) -> dict:
    path = expected_path or urlsplit(LISTING_URL).path
    check_origin(response_url, path)
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = ""
    for node in soup.select("h1,h2,h3"):
        text = normalized(node.get_text(" ", strip=True))
        if text:
            heading = text
            break
    rows, issues, seen = [], [], set()
    for card in soup.select(f"div.{CARD_CLASS}"):
        fields = _card_fields(card)
        code = fields["code"]
        if not JOB_CODE.fullmatch(code):
            issues.append(f"listing_row_missing_or_bad_job_code:{code or 'empty'}")
            continue
        if code in seen:
            issues.append(f"listing_duplicate_job_code:{code}")
            continue
        if not fields["title"]:
            issues.append(f"listing_row_missing_title:{code}")
        if not fields["button_name"]:
            issues.append(f"listing_row_missing_apply_postback:{code}")
        fields["order"] = len(rows)
        fields["page_url"] = response_url
        rows.append(fields)
        seen.add(code)
    pager = [normalized(a.get_text(" ", strip=True)) for a in soup.select("div.col-md-12.mt-2.mb-4 a")]
    no_jobs = "no jobs available" in normalized_body_text(html).lower()
    return {
        "url": response_url,
        "page_title": page_title,
        "heading": heading,
        "rows": rows,
        "job_ids": [row["code"] for row in rows],
        "count": len(rows),
        "unique_count": len({row["code"] for row in rows}),
        "pager_labels": pager,
        "no_jobs_message": no_jobs,
        "issues": issues,
    }


def parse_bps_page(html: str, response_url: str = BPS_URL) -> dict:
    parsed = parse_listing(html, response_url, expected_path=urlsplit(BPS_URL).path)
    parsed["search_control_present"] = bool(
        BeautifulSoup(html, "lxml").select_one(f'input[name="{BPS_SEARCH_BUTTON}"]')
    )
    parsed["error_500"] = error_page(html, response_url)
    return parsed


def parse_bps_search_response(html: str, response_url: str) -> dict:
    return {
        "url": response_url,
        "error_500": error_page(html, response_url),
        "final_path": urlsplit(response_url).path,
        "page_title": normalized(BeautifulSoup(html, "lxml").title.get_text(" ", strip=True))
        if BeautifulSoup(html, "lxml").title else "",
    }


def form_state(html: str) -> dict[str, str]:
    """Recreate the browser's ASP.NET WebForms POST state."""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", {"name": "aspnetForm"}) or soup.find("form")
    if form is None:
        raise ValueError("Tech Mahindra page no longer contains its ASP.NET form")
    data: dict[str, str] = {}
    for node in form.select("input"):
        name = node.get("name")
        if not name:
            continue
        kind = (node.get("type") or "text").lower()
        if kind in {"submit", "button", "image", "file", "reset"}:
            continue
        if kind in {"checkbox", "radio"} and not node.has_attr("checked"):
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


def parse_date(value: str | None) -> tuple[str | None, str | None]:
    """Parse the site's DD/MM/YYYY value; never substitute a request date."""
    raw = normalized(value)
    if not raw:
        return None, "empty_date"
    match = DATE_VALUE.fullmatch(raw)
    if match is None:
        return None, f"unparseable_date:{raw[:40]}"
    day, month, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d"), None
    except ValueError:
        return None, f"invalid_calendar_date:{raw}"


def _location(value: str | None) -> dict:
    raw = normalized(value)
    match = LOCATION_VALUE.match(raw)
    if match:
        return {"raw": raw, "city": normalized(match.group(1)), "country": normalized(match.group(2))}
    return {"raw": raw, "city": raw or None, "country": None}


SECTION_HEADING = re.compile(
    r"^(?:job summary|job title|location|onsite|remote|hybrid|jd|key responsibilities|"
    r"responsibilities|roles and responsibilities|role description|mandatory skills|"
    r"preferred skills|qualifications?|qualification\s*&\s*experience|key skills|"
    r"must-have|must have|nice to have|who you are|requirements|experience request|"
    r"skills required|professional skills|leadership skills|trainings?|duties|"
    r"about (?:the )?role|what you(?:'|’)ll do|what we(?:'|’)re looking for)",
    re.I,
)
EXACT_HEADINGS = {
    "job summary", "job title", "location", "onsite/ remote", "remote", "hybrid",
    "key responsibilities", "responsibilities", "roles and responsibilities",
    "role description", "mandatory skills", "preferred skills", "qualifications",
    "qualification", "key skills required", "must-have skills", "must have skills",
    "nice to have", "who you are", "requirements", "skills required",
    "professional skills", "leadership skills", "trainings & certification",
    "roles and responsibilities", "job description", "about the role",
    "what you'll do", "what we're looking for", "jd",
}


FIELD_LABELS = {"job title", "location", "onsite/ remote", "remote", "hybrid"}


def _heading_line(line: str) -> bool:
    lower = line.lower().rstrip(":").strip()
    if lower in EXACT_HEADINGS and ":" not in line:
        return True
    match = re.match(r"^([^:]{2,60}):\s*(.*)$", line)
    if match:
        label, value = match.group(1).strip(), match.group(2).strip()
        if label.lower().strip() in FIELD_LABELS and value:
            return False
        if SECTION_HEADING.match(label) and len(value) <= 30:
            return True
        if len(line) <= 40 and line.endswith(":"):
            return True
    return False


def _text_sections(text: str) -> list[dict]:
    """Split a rendered Job Summary at its published heading lines.

    The portal emits some summaries with nested ``<h1>``/``<h4>`` headings, but
    the lxml tree normalises several of those into paragraphs.  The visible text
    still keeps each heading on its own line, so the section boundaries are read
    from the text without inventing or dropping any body content.
    """
    lines = [normalized(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    sections: list[dict] = []
    heading: str | None = None
    parts: list[str] = []

    def flush():
        if heading is not None:
            value = normalized(" ".join(parts))
            if value:
                sections.append({"heading": heading, "text": value})

    for line in lines:
        if _heading_line(line):
            flush()
            heading = line.rstrip(":")
            parts = []
        elif heading is not None:
            parts.append(line)
        else:
            heading = "Job Summary"
            parts = [line]
    flush()
    return sections


def _salary_sentence(text: str) -> dict | None:
    for pattern in (
        r"(?i)((?:the\s+)?pay range[^.]{0,240}\.?)",
        r"(?i)((?:the\s+)?(?:salary|compensation)[^.]{0,240}\.?)",
    ):
        match = re.search(pattern, text or "")
        if match:
            value = normalized(match.group(1))
            if value:
                return {"source_text": value}
    return None


def parse_detail_page(html: str, response_url: str = DETAIL_URL, listing: dict | None = None) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != HOST:
        raise ValueError(f"Tech Mahindra detail response came from an unexpected origin: {response_url}")
    if blocked(html):
        raise SourceBlocked("Tech Mahindra detail route returned a WAF/challenge page")
    if error_page(html, response_url):
        raise DetailUnavailable(f"Tech Mahindra detail route returned a site error page: {response_url}")
    if parts.path != urlsplit(DETAIL_URL).path:
        raise DetailUnavailable(f"Tech Mahindra detail route changed: {response_url}")
    if COUNTRY_BLOCK_MARKER in normalized_body_text(html).lower():
        raise DetailUnavailable(
            "publisher_country_block: the official detail page says "
            "'This job does not belong to your country'"
        )
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.find(id=DETAIL_TITLE_ID)
    if title_node is None:
        raise DetailUnavailable("Tech Mahindra detail page no longer publishes its title")
    title = normalized(title_node.get_text(" ", strip=True))
    if not title:
        raise DetailUnavailable("Tech Mahindra detail page title is empty")
    degree = soup.find(id=DETAIL_DEGREE_ID)
    degree_note = normalized(degree.get_text(" ", strip=True)) if degree else None

    metadata: dict[str, str] = {}
    metadata_issues: list[str] = []
    section = soup.find(id=DETAIL_SECTION_ID)
    if section is None:
        metadata_issues.append("detail_missing_job_description_section")
    else:
        for item in section.select("ul.skillset li"):
            label_node = item.find("b")
            value_node = item.select_one("span.red")
            if label_node is None or value_node is None:
                metadata_issues.append("detail_metadata_row_missing_label_or_value")
                continue
            label = normalized(label_node.get_text(" ", strip=True)).rstrip(" :")
            metadata[label] = normalized(value_node.get_text(" ", strip=True))
    for label in METADATA_LABELS:
        if label not in metadata:
            metadata_issues.append(f"detail_missing_metadata:{label}")

    summary_heading = None
    for node in soup.find_all("h3"):
        if normalized(node.get_text(" ", strip=True)).lower() == "job summary":
            summary_heading = node
            break
    summary_container = summary_heading.find_parent("div", class_="col-md-12") if summary_heading else None
    if summary_container is None and section is not None:
        # The heading is normally inside #..._JDSetion's col-md-12 child.  Do
        # not fall back to the whole page (that would retain the footer).
        summary_container = section.select_one("div.col-md-12.mt-4.border-top")
    if summary_container is None:
        summary_html = ""
        description_text = ""
        description_normalized = ""
        sections: list[dict] = []
    else:
        summary_html = "".join(str(child) for child in summary_container.contents).strip()
        description_text = text_html(summary_html)
        description_normalized = normalized(description_text)
        sections = _text_sections(description_text)
    if not summary_heading:
        metadata_issues.append("detail_missing_job_summary_heading")
    if not description_normalized:
        metadata_issues.append("detail_empty_job_summary")

    posted_raw = metadata.get("Job Post Date", "")
    date_posted, date_issue = parse_date(posted_raw)
    if date_issue and date_issue != "empty_date":
        metadata_issues.append(f"detail_posted_date:{date_issue}")
    expiry_raw = metadata.get("Job Expiry Date", "")
    valid_through, expiry_issue = parse_date(expiry_raw)
    if expiry_issue and expiry_issue != "empty_date":
        metadata_issues.append(f"detail_expiry_date:{expiry_issue}")

    openings = metadata.get("No of Openings")
    openings_value = None
    if openings:
        match = re.search(r"\d+", openings)
        if match:
            openings_value = int(match.group(0))
        else:
            metadata_issues.append(f"detail_openings_not_numeric:{openings[:40]}")

    experience = metadata.get("Total Experience")
    experience_value = None
    if experience:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)", experience)
        experience_value = {
            "raw": experience,
            "min_years": float(match.group(1)) if match else None,
            "max_years": float(match.group(2)) if match else None,
        }

    listing_title = normalized(listing.get("title")) if listing else ""
    if listing_title and normalized(title) != listing_title:
        metadata_issues.append(f"detail_title_mismatch:{listing_title}->{title}")
    listing_location = normalized(listing.get("listing_location_text")) if listing else ""
    detail_location = _location(metadata.get("Location"))
    if listing_location and detail_location["city"] and listing_location != detail_location["city"]:
        metadata_issues.append(
            f"detail_location_city_mismatch:{listing_location}->{detail_location['city']}"
        )

    apply_controls = [
        {"name": node.get("name"), "value": node.get("value"), "id": node.get("id")}
        for node in soup.select('input[type="submit"][name*="pply"], input[type="submit"][name*="hortlist"]')
    ]
    responsibilities = [
        item for item in sections if SECTION_RESPONSIBILITY.search(item["heading"])
    ] or None
    qualifications = [
        item for item in sections if SECTION_QUALIFICATION.search(item["heading"])
    ] or None
    salary = _salary_sentence(description_normalized)
    eeo_marker = "tech mahindra is an equal employment opportunity employer"
    eeo_index = description_normalized.lower().find(eeo_marker)
    boilerplate_tail = (
        description_normalized[eeo_index:].strip() if eeo_index >= 0 else None
    )

    return {
        "url": response_url,
        "title": title,
        "degree_note": degree_note,
        "metadata": metadata,
        "metadata_issues": metadata_issues,
        "posted_date_raw": posted_raw or None,
        "date_posted": date_posted,
        "expiry_date_raw": expiry_raw or None,
        "valid_through": valid_through,
        "no_of_openings": openings_value,
        "experience": experience_value,
        "job_reference_no": metadata.get("Job Reference No"),
        "domain": metadata.get("Domain"),
        "location": detail_location,
        "skills": metadata.get("Skill Set"),
        "summary_heading_present": bool(summary_heading),
        "summary_html": summary_html,
        "description_text": description_text,
        "description_normalized": description_normalized,
        "description_normalized_chars": len(description_normalized),
        "description_fnv1a64": fnv1a64(description_normalized),
        "description_sha256": sha256(description_normalized.encode()).hexdigest() if description_normalized else None,
        "description_unavailable": not description_normalized,
        "sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "salary": salary,
        "boilerplate_tail": boilerplate_tail,
        "apply_controls": apply_controls,
        "issues": list(dict.fromkeys(metadata_issues)),
    }


class TechMahindra(Adapter):
    """Enumerate and hydrate every public posting exposed by Tech Mahindra."""

    VERIFICATION_REVISION = "techmahindra.careers.webforms-free-search-detail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.baseline_state: dict[str, str] | None = None
        self.baseline_button: str | None = None
        self.baseline_code_input: str | None = None

    async def _response(self, method: str, url: str, **kwargs):
        response = await self.http.request(method, url, **kwargs)
        if blocked(response.text, response.status_code):
            raise SourceBlocked(
                f"Tech Mahindra returned a WAF/challenge/rate-limit response: "
                f"{response.status_code} {response.url}"
            )
        return response

    async def _list(self, url: str, *, kind: str, **kwargs) -> dict:
        response = await self._response("GET", url, **kwargs)
        parsed = parse_listing(response.text, str(response.url),
                               expected_path=urlsplit(url).path)
        parsed.update({
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "kind": kind,
        })
        return parsed

    def _set_baseline(self, html: str) -> None:
        cards = BeautifulSoup(html, "lxml").select(f"div.{CARD_CLASS}")
        if not cards:
            raise ValueError("Tech Mahindra listing no longer publishes a 12-card baseline")
        fields = _card_fields(cards[0])
        if not fields["button_name"] or not fields["code_input_name"]:
            raise ValueError("Tech Mahindra listing baseline card has no postback controls")
        self.baseline_button = fields["button_name"]
        self.baseline_code_input = fields["code_input_name"]
        self.baseline_state = form_state(html)

    async def _walk_default_pager(self, first_html: str, first_listing: dict) -> dict:
        """Walk the official 'Latest IT Jobs' pager to record its real overlap.

        The default view is only a subset of the full free-search membership, but
        its five page requests are part of the published UI and several codes
        repeat across pages because the server's order is not stable.  This walk
        records that exact behaviour; it never replaces the free-search union.
        """
        started = time.perf_counter()
        pages = [{
            "page": 1,
            "rows": first_listing["rows"],
            "row_count": first_listing["count"],
            "unique": first_listing["unique_count"],
            "codes": first_listing["job_ids"],
            "response_sha256": first_listing.get("response_sha256"),
        }]
        issues = []
        current_html = first_html
        state = form_state(first_html)
        for target_page in range(2, 6):
            soup = BeautifulSoup(current_html, "lxml")
            anchor = None
            for node in soup.select("div.col-md-12.mt-2.mb-4 a"):
                if normalized(node.get_text(" ", strip=True)) == str(target_page):
                    anchor = node
                    break
            if anchor is None:
                issues.append(f"default_pager_has_no_link_for_page:{target_page}")
                break
            onclick = anchor.get("onclick") or anchor.get("href") or ""
            match = re.search(r"__doPostBack\('([^']+)'", onclick)
            if match is None:
                issues.append(f"default_pager_link_has_no_postback:{target_page}")
                break
            data = dict(state)
            data["__EVENTTARGET"] = match.group(1)
            data["__EVENTARGUMENT"] = ""
            response = await self._response(
                "POST", LISTING_URL, data=data,
                headers={"Referer": LISTING_URL, "Origin": OFFICIAL_ENTRY.rstrip("/")},
            )
            parsed = parse_listing(response.text, str(response.url),
                                   expected_path=urlsplit(LISTING_URL).path)
            after = BeautifulSoup(response.text, "lxml")
            disabled = after.select_one("a.page_disabled")
            disabled_label = normalized(disabled.get_text(" ", strip=True)) if disabled else None
            pages.append({
                "page": target_page,
                "rows": parsed["rows"],
                "row_count": parsed["count"],
                "unique": parsed["unique_count"],
                "codes": parsed["job_ids"],
                "disabled_label": disabled_label,
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if disabled_label != str(target_page):
                issues.append(
                    f"default_pager_did_not_advance:{target_page}->{disabled_label}"
                )
                break
            state = form_state(response.text)
            current_html = response.text
        all_codes = [code for page in pages for code in page["codes"]]
        report = {
            "pages": pages,
            "page_requests": len(pages),
            "rows_observed": len(all_codes),
            "unique_codes": len(set(all_codes)),
            "duplicate_rows": len(all_codes) - len(set(all_codes)),
            "pager_labels_first_page": first_listing["pager_labels"],
            "issues": issues,
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }
        return report

    async def _free_search(self) -> dict:
        if self.baseline_state is None:
            raise ValueError("Tech Mahindra free search requires the listing baseline")
        data = dict(self.baseline_state)
        data[FREE_SEARCH_BUTTON] = "Search"
        data[FREE_SEARCH_INPUT] = ""
        response = await self._response(
            "POST", LISTING_URL, data=data,
            headers={"Referer": LISTING_URL, "Origin": OFFICIAL_ENTRY.rstrip("/")},
        )
        parsed = parse_listing(response.text, str(response.url),
                               expected_path=urlsplit(LISTING_URL).path)
        parsed.update({
            "status": response.status_code,
            "request_method": "POST",
            "request_form_fields": len(data),
            "request_form_values": {
                "__EVENTTARGET": data.get("__EVENTTARGET", ""),
                "btnFreeSearch": "Search",
                "txtAdvanceSearch": "",
            },
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _select_and_detail(self, job) -> None:
        listing = job.raw["listing"]
        code = listing["code"]
        data = dict(self.baseline_state or {})
        if self.baseline_button is None or self.baseline_code_input is None:
            raise DetailUnavailable("Tech Mahindra detail postback controls were not established")
        data[self.baseline_button] = "Apply/Shortlist"
        data[self.baseline_code_input] = code
        selected = await self._response(
            "POST", LISTING_URL, data=data,
            headers={"Referer": LISTING_URL, "Origin": OFFICIAL_ENTRY.rstrip("/")},
        )
        if error_page(selected.text, str(selected.url), selected.status_code):
            raise DetailUnavailable(
                f"Tech Mahindra Apply/Shortlist postback returned an error page for {code}"
            )
        detail = await self._response(
            "GET", DETAIL_URL, headers={"Referer": LISTING_URL},
        )
        parsed = parse_detail_page(detail.text, str(detail.url), listing)
        parsed.update({
            "status": detail.status_code,
            "response_bytes": len(detail.content),
            "response_sha256": sha256(detail.content).hexdigest(),
            "selection": "official Apply/Shortlist postback selected HdnJobCode",
            "listing_job_code": code,
            "detail_route": DETAIL_URL,
            "detail_route_is_session_selected": True,
        })
        job.raw["detail"] = parsed
        job.raw["detail_source"] = (
            "Official JobDetails.aspx after the listing card's Apply/Shortlist postback; "
            "the route is session-selected and the site publishes no per-posting permalink."
        )
        job.title = parsed["title"] or job.title
        job.requisition_id = parsed["job_reference_no"] or None
        job.department = parsed["domain"] or job.department
        job.description_html = parsed["summary_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "techmahindra.careers.jobdetails.job_summary"
        job.description_source_url = DETAIL_URL
        job.description_unavailable = parsed["description_unavailable"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["posted_date_raw"]
        job.valid_through = parsed["valid_through"]
        job.experience = parsed["experience"]
        job.qualifications = parsed["qualifications"] or (
            {"skill_set": parsed["skills"], "degree_note": parsed["degree_note"],
             "total_experience": parsed["experience"]}
            if (parsed["skills"] or parsed["degree_note"] or parsed["experience"]) else None
        )
        job.responsibilities = parsed["responsibilities"]
        job.salary = parsed["salary"]
        job.locations = [parsed["location"]] if parsed["location"].get("raw") else job.locations
        job.apply_url = DETAIL_URL
        job.employer = "Tech Mahindra Limited"
        job.language = "en"
        if parsed["description_unavailable"]:
            job.warnings.append("publisher_empty_description")
        elif parsed["description_normalized_chars"] < 100:
            job.warnings.append("publisher_short_description")
        for issue in parsed["issues"]:
            if issue.startswith(("detail_title_mismatch", "detail_location_city_mismatch",
                                 "detail_missing_metadata", "detail_empty_job_summary",
                                 "detail_missing_job_summary_heading")):
                self.result.errors.append(f"Tech Mahindra source mismatch [{code}]: {issue}")
                job.warnings.append(issue)
        for key in ("no_of_openings", "metadata", "degree_note", "sections",
                    "job_reference_no", "detail_route", "detail_route_is_session_selected"):
            if key not in job.raw:
                job.raw[key] = parsed.get(key)
        job.raw["no_of_openings"] = parsed["no_of_openings"]
        job.raw["metadata"] = parsed["metadata"]
        job.raw["degree_note"] = parsed["degree_note"]
        job.raw["sections"] = parsed["sections"]
        job.raw["boilerplate_tail"] = parsed["boilerplate_tail"]
        job.raw["detail_route"] = DETAIL_URL
        job.raw["detail_route_is_session_selected"] = True
        self.result.validate_job(job)

    async def _hydrate_sequential(self, jobs) -> dict:
        started = time.perf_counter()
        stats = {
            "total": len(jobs), "checked": 0, "fetched_jobs": 0, "failed": 0,
            "publisher_empty": 0, "blocked": False,
        }
        self.result.metadata["details"] = stats
        for index, job in enumerate(jobs):
            if self.options.max_jobs and index >= self.options.max_jobs:
                self.result.warnings.append("sample_limit")
                self.result.listing_complete = False
                break
            try:
                await self._select_and_detail(job)
                stats["fetched_jobs"] += 1
            except SourceBlocked as exc:
                stats["blocked"] = True
                stats["failed"] += 1
                job.warnings.append("detail_fetch_blocked")
                self.result.errors.append(f"Tech Mahindra detail blocked [{job.job_id}]: {exc}")
                self.result.listing_complete = False
                for remaining in jobs[index + 1:]:
                    remaining.warnings.append("detail_not_attempted_after_block")
                    stats["failed"] += 1
                break
            except DetailUnavailable as exc:
                stats["failed"] += 1
                job.warnings.append("detail_fetch_failed")
                if "publisher_country_block" in str(exc):
                    job.warnings.append("publisher_country_block")
                self.result.errors.append(f"Tech Mahindra detail unavailable [{job.job_id}]: {exc}")
            except Exception as exc:
                stats["failed"] += 1
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(
                    f"Tech Mahindra detail failed [{job.job_id}]: {type(exc).__name__}: {exc}"
                )
            if job.description_unavailable:
                stats["publisher_empty"] += 1
            self.result.validate_job(job)
            self.result.dirty_jobs.append(job)
            stats["checked"] = index + 1
            if self.options.checkpoint and stats["checked"] % 25 == 0:
                self.options.checkpoint(self.result)
        stats["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        self.result.timings["detail_phase_seconds"] = (
            self.result.timings.get("detail_phase_seconds", 0.0) + max(0.0, time.perf_counter() - started)
        )
        return stats

    def _stub_job(self, row: dict):
        return self.job(
            {
                "listing": row,
                "listing_url": LISTING_URL,
                "detail_route": DETAIL_URL,
                "detail_route_is_session_selected": True,
                "employer_provenance": (
                    "Tech Mahindra Limited is the employer declared in the official careers "
                    "site footer; postings may state a direct-payroll engagement in their body."
                ),
            },
            title=row["title"],
            url=DETAIL_URL,
            job_id=row["code"],
            requisition_id=None,
            apply_url=DETAIL_URL,
            description="",
            method=None,
            description_url=None,
            locations=[{"raw": row["listing_location_text"],
                        "city": row["listing_location_text"], "country": None}]
            if row.get("listing_location_text") else [],
            department=row.get("category") or "IT",
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer="Tech Mahindra Limited",
            language="en",
        )

    async def run(self):  # noqa: C901 - explicit source journey
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "bps_url": BPS_URL,
            "detail_url": DETAIL_URL,
            "listing_authority": (
                "Official Tech Mahindra IT Opportunities page: its unfiltered free-text "
                "search returns every public IT posting (490 unique codes at capture). The "
                "default 'Latest IT Jobs' 12-row view is a 60-row subset whose pages overlap "
                "(36 unique codes over five pages) and is not the membership authority. The "
                "BPS Opportunities page publishes no current rows."
            ),
            "detail_method": (
                "Each listing card's official Apply/Shortlist postback selects its HdnJobCode "
                "in the server session; JobDetails.aspx then renders the full public fields "
                "and Job Summary. No account/application was submitted."
            ),
            "request_scope": (
                "Only careers.techmahindra.com's own entry, IT/BPS listing, free-search and "
                "JobDetails.aspx routes; no ATS board, parent-company board, aggregator, "
                "guessed slug or CAPTCHA bypass is used."
            ),
        })
        try:
            entry_started = time.perf_counter()
            entry_response = await self._response("GET", OFFICIAL_ENTRY)
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            entry.update({
                "status": entry_response.status_code,
                "response_bytes": len(entry_response.content),
                "response_sha256": sha256(entry_response.content).hexdigest(),
                "seconds": max(0.0, time.perf_counter() - entry_started),
            })
            self.result.metadata["initial_official_entry"] = entry

            listing_started = time.perf_counter()
            listing_response = await self._response("GET", LISTING_URL)
            listing = parse_listing(listing_response.text, str(listing_response.url),
                                    expected_path=urlsplit(LISTING_URL).path)
            listing.update({
                "status": listing_response.status_code,
                "response_bytes": len(listing_response.content),
                "response_sha256": sha256(listing_response.content).hexdigest(),
                "seconds": max(0.0, time.perf_counter() - listing_started),
            })
            self.result.metadata["initial_default_listing"] = listing
            self._set_baseline(listing_response.text)
            pager_walk = await self._walk_default_pager(listing_response.text, listing)
            self.result.metadata["default_listing_pager_walk"] = pager_walk
            self.result.pages += pager_walk["page_requests"] - 1
            if pager_walk["issues"]:
                self.result.warnings.extend(
                    f"Tech Mahindra default pager: {issue}" for issue in pager_walk["issues"]
                )

            free_started = time.perf_counter()
            free = await self._free_search()
            free["seconds"] = max(0.0, time.perf_counter() - free_started)
            self.result.metadata["initial_free_search"] = free

            bps_started = time.perf_counter()
            bps_response = await self._response("GET", BPS_URL)
            bps = parse_bps_page(bps_response.text, str(bps_response.url))
            bps.update({
                "status": bps_response.status_code,
                "response_bytes": len(bps_response.content),
                "response_sha256": sha256(bps_response.content).hexdigest(),
                "seconds": max(0.0, time.perf_counter() - bps_started),
            })
            self.result.metadata["initial_bps_listing"] = bps

            bps_search = None
            try:
                bps_state = form_state(bps_response.text)
                bps_state[BPS_SEARCH_BUTTON] = "Search"
                bps_search_response = await self._response(
                    "POST", BPS_URL, data=bps_state,
                    headers={"Referer": BPS_URL, "Origin": OFFICIAL_ENTRY.rstrip("/")},
                )
                bps_search = parse_bps_search_response(
                    bps_search_response.text, str(bps_search_response.url)
                )
                bps_search.update({
                    "status": bps_search_response.status_code,
                    "response_bytes": len(bps_search_response.content),
                    "response_sha256": sha256(bps_search_response.content).hexdigest(),
                })
                if bps_search["error_500"]:
                    self.result.warnings.append("bps_empty_search_returns_site_500")
                    self.result.metadata["bps_search_error"] = bps_search
            except SourceBlocked as exc:
                self.result.warnings.append(f"bps_search_blocked:{exc}")
                self.result.metadata["bps_search_error"] = {"blocked": str(exc)}
            except Exception as exc:
                self.result.warnings.append(f"bps_search_failed:{type(exc).__name__}: {exc}")
                self.result.metadata["bps_search_error"] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }

            rows = list(free["rows"])
            free_codes = {row["code"] for row in rows}
            pager_rows = [row for page in pager_walk["pages"] for row in page["rows"]]
            pager_codes = {row["code"] for row in pager_rows}
            pager_only_codes = sorted(pager_codes - free_codes)
            pager_walk["default_codes_not_in_free_search"] = pager_only_codes
            if pager_only_codes:
                self.result.warnings.append(
                    f"tech_mahindra_default_pager_has_codes_outside_free_search:"
                    f"{len(pager_only_codes)}"
                )
                seen_free = set(free_codes)
                for row in pager_rows:
                    if row["code"] in seen_free:
                        continue
                    item = dict(row)
                    item["source_listing"] = "IT Opportunities default pager (not in free search)"
                    rows.append(item)
            if bps["rows"]:
                for row in bps["rows"]:
                    row = dict(row)
                    row["category"] = row.get("category") or "BPS"
                    row["source_listing"] = "BPS Opportunities"
                    rows.append(row)
            unique_rows, seen = [], set()
            for row in rows:
                if row["code"] in seen:
                    self.result.warnings.append(f"duplicate_listing_code:{row['code']}")
                    continue
                seen.add(row["code"])
                row["source_listing"] = row.get("source_listing") or "IT Opportunities free search"
                unique_rows.append(row)
            self.result.metadata["membership"] = {
                "it_free_search_unique": free["unique_count"],
                "it_free_search_rows": free["count"],
                "default_listing_rows": pager_walk["rows_observed"],
                "default_listing_unique": pager_walk["unique_codes"],
                "default_listing_pages": pager_walk["page_requests"],
                "default_listing_pager": listing["pager_labels"],
                "default_listing_overlap_note": (
                    "The five default pager requests returned "
                    f"{pager_walk['rows_observed']} rows / "
                    f"{pager_walk['unique_codes']} unique codes in this run. The order "
                    "is unstable across requests (an earlier review walk observed 36 "
                    "unique of 60), so the unfiltered free search is the membership "
                    "authority; any pager-only code is unioned into the membership."
                ),
                "default_pager_codes_not_in_free_search": pager_only_codes,
                "bps_rows": bps["count"],
                "bps_no_jobs_message": bps["no_jobs_message"],
                "bps_search_error": bool(bps_search and bps_search.get("error_500")),
                "union_rows": len(unique_rows),
                "union_unique": len({row['code'] for row in unique_rows}),
            }
            if free["issues"]:
                self.result.errors.extend(f"Tech Mahindra free search: {issue}" for issue in free["issues"])
            if listing["issues"]:
                self.result.warnings.extend(f"Tech Mahindra default listing: {issue}" for issue in listing["issues"])
            if bps["issues"]:
                self.result.warnings.extend(f"Tech Mahindra BPS listing: {issue}" for issue in bps["issues"])
            if not free["rows"]:
                self.result.errors.append("tech_mahindra_free_search_returned_no_jobs")
                self.result.listing_complete = False
                self.result.expected_total = 0
                return self.result

            self.result.expected_total = len(unique_rows)
            self.result.pages += 1  # entry + default listing + free search + BPS
            jobs = []
            for row in unique_rows:
                job = self._stub_job(row)
                self.add(job)
                jobs.append(job)

            await self._hydrate_sequential(jobs)

            # Live membership recheck after detail hydration.
            recheck_started = time.perf_counter()
            recheck_error = None
            try:
                recheck_listing_response = await self._response("GET", LISTING_URL)
                self._set_baseline(recheck_listing_response.text)
                recheck_free = await self._free_search()
            except Exception as exc:
                recheck_error = f"{type(exc).__name__}: {exc}"
                recheck_free = None
            recheck = {
                "seconds": max(0.0, time.perf_counter() - recheck_started),
                "error": recheck_error,
                "initial_codes": [row["code"] for row in unique_rows],
            }
            if recheck_free is not None:
                current_codes = [row["code"] for row in recheck_free["rows"]]
                initial = set(recheck["initial_codes"])
                current = set(current_codes)
                new_codes = [code for code in current_codes if code not in initial]
                missing_codes = [code for code in recheck["initial_codes"] if code not in current]
                recheck.update({
                    "current_rows": len(current_codes),
                    "current_unique": len(current),
                    "new_codes": new_codes,
                    "missing_codes": missing_codes,
                    "stable": not new_codes and not missing_codes,
                })
                if new_codes:
                    # Collect newly published boundary postings once, preserving
                    # the original capture and labelling the delta.
                    delta_jobs = []
                    for code in new_codes:
                        row = next(row for row in recheck_free["rows"] if row["code"] == code)
                        job = self._stub_job(row)
                        self.add(job)
                        delta_jobs.append(job)
                    await self._hydrate_sequential(delta_jobs)
                    jobs.extend(delta_jobs)
                    self.result.expected_total = len(jobs)
                if missing_codes:
                    self.result.warnings.append(
                        f"tech_mahindra_postings_removed_during_run:{len(missing_codes)}"
                    )
            else:
                self.result.errors.append(f"Tech Mahindra membership recheck failed: {recheck_error}")
                self.result.listing_complete = False
            self.result.metadata["membership_recheck"] = recheck
            self.result.timings["recheck_phase_seconds"] = recheck["seconds"]

            if not self.result.listing_complete:
                self.result.listing_complete = (
                    recheck.get("stable", False)
                    and not self.result.metadata.get("details", {}).get("blocked", False)
                )
            if self.result.metadata.get("details", {}).get("blocked"):
                self.result.listing_complete = False
            detail_failed = [
                job for job in self.result.jobs
                if "detail_fetch_failed" in job.warnings or "detail_fetch_blocked" in job.warnings
            ]
            if detail_failed:
                self.result.errors.append(
                    f"tech_mahindra_detail_failures:{len(detail_failed)}"
                )
            self.result.metadata["detail_failures"] = [
                {"job_id": job.job_id, "title": job.title, "url": job.url,
                 "warnings": job.warnings}
                for job in detail_failed
            ]
        except SourceBlocked as exc:
            self.result.errors.append(f"Tech Mahindra source blocked: {exc}")
            self.result.listing_complete = False
        except Exception as exc:
            self.result.errors.append(f"Tech Mahindra source failure: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        finally:
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.metadata["timings"] = {
                key: round(value, 6) for key, value in self.result.timings.items()
            }
        return self.result
