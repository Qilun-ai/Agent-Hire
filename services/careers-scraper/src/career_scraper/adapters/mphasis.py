"""Mphasis's own careers site, its official RippleHire redirect and candidate APIs."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import html as html_lib
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.mphasis.com/"
OFFICIAL_LISTING_REDIRECT = "https://careers.mphasis.com/home/mphasis-careers.html"
RIPPLEHIRE_JOBS_LINK = "https://mphasis.ripplehire.com/ripplehire/careers"
CANDIDATE_ORIGIN = "https://mphasis.ripplehire.com"
CANDIDATE_PATH = "/candidate/"
SEARCH_API = CANDIDATE_ORIGIN + "/candidate/candidatejobsearch"
DETAIL_API = CANDIDATE_ORIGIN + "/candidate/candidatejobdetail"
PAGE_SIZE = 10
SOURCE_FLAG = "CAREERSITE"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
SALARY_RANGE = re.compile(
    r"Salary\s*(?:range)?\s*[:\-]?\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([Kk])?\s*"
    r"(?:to|–|-)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([Kk])?",
    re.I,
)
SALARY_SINGLE = re.compile(
    r"Salary\s*[:\-]\s*(\$?\s*[\d,]+(?:\.\d+)?(?:\s*[Kk])?(?:\s*/\s*(?:Annum|Year|Month|Hour))?)",
    re.I,
)


def _decode(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    # The candidate API occasionally emits duplicate Windows-1252 bytes inside
    # an otherwise UTF-8 body (for example C2 AE AE for a single registered
    # sign). The rendered page drops the stray byte, so decoding with
    # errors="ignore" preserves the visible text. Raw bytes stay in responses/.
    return content.decode("utf-8-sig", errors="ignore")


def _text(node: Tag | None) -> str:
    return node.get_text() if node is not None else ""


def _field(node: Tag | None, tag: str) -> str:
    return _text(node.find(tag)) if node is not None and node.find(tag) is not None else ""


def _inner_tag(value: str, tag: str) -> str:
    match = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", value, re.S | re.I)
    return match.group(1) if match else ""


def _decode_js_url(value: str) -> str:
    value = value.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
    value = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), value)
    return html_lib.unescape(value)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_text(value: str | None) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value.strip()


def _wrap_orphan_list_items(soup: BeautifulSoup) -> None:
    body = soup.body or soup
    for item in list(body.find_all("li")):
        parent = item.parent
        if not isinstance(parent, Tag) or parent.name in ("ul", "ol", "li", "p"):
            continue
        group = []
        for sibling in list(parent.children):
            if isinstance(sibling, Tag) and sibling.name == "li":
                group.append(sibling)
            elif group:
                wrapper = soup.new_tag("ul")
                group[0].insert_before(wrapper)
                for node in group:
                    wrapper.append(node.extract())
                group = []
        if group:
            wrapper = soup.new_tag("ul")
            group[0].insert_before(wrapper)
            for node in group:
                wrapper.append(node.extract())


def clean_fragment(value: str | None) -> str:
    """Decode the one HTML layer used by RippleHire and repair its broken tag endings."""
    if not value:
        return ""
    value = html_lib.unescape(value)
    # RippleHire stores some source bodies with a dangling '<' at line ends.
    # The rendered page drops those characters; preserve the same visible body.
    value = re.sub(r"</(?=\s*\n\s*(?:<|$))", "", value)
    value = re.sub(r"<(?=\s*\n)", "", value)
    value = re.sub(r"</(?=\s*</)", "", value)
    value = re.sub(r"<(?=\s*</)", "", value)
    value = re.sub(r"<\s*$", "", value)
    soup = BeautifulSoup(value, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    _wrap_orphan_list_items(soup)
    body = soup.body
    return "".join(str(node) for node in body.contents).strip() if body else str(soup)


def parse_date(value: str | None) -> str | None:
    match = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*", value or "")
    if not match:
        return None
    day, month, year = int(match.group(1)), MONTHS.get(match.group(2).lower()), int(match.group(3))
    if month is None:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_salary(other_text: str, description_text: str, currency: str) -> dict | None:
    for source, text in (("otherDetails", other_text), ("description", description_text)):
        if not text:
            continue
        match = SALARY_RANGE.search(text)
        if match:
            low = float(match.group(1).replace(",", ""))
            high = float(match.group(3).replace(",", ""))
            if match.group(2):
                low *= 1000
            if match.group(4):
                high *= 1000
            currency_code = "USD" if "$" in match.group(0) else ("INR" if "Rs" in match.group(0) else currency.upper())
            return {
                "text": match.group(0).strip(),
                "min": int(low) if low.is_integer() else low,
                "max": int(high) if high.is_integer() else high,
                "currency": currency_code,
                "period": "year",
                "source": source,
            }
        match = SALARY_SINGLE.search(text)
        if match:
            token = match.group(1).strip()
            amount_match = re.search(r"([\d,]+(?:\.\d+)?)\s*([Kk])?", token)
            if not amount_match:
                continue
            amount = float(amount_match.group(1).replace(",", ""))
            if amount_match.group(2):
                amount *= 1000
            period = "year" if re.search(r"annum|year", token, re.I) else (
                "month" if re.search(r"month", token, re.I) else (
                    "hour" if re.search(r"hour", token, re.I) else None))
            currency_code = "USD" if "$" in token else ("INR" if "Rs" in token else currency.upper())
            return {
                "text": token,
                "min": int(amount) if amount.is_integer() else amount,
                "max": int(amount) if amount.is_integer() else amount,
                "currency": currency_code,
                "period": period,
                "source": source,
            }
    return None


def parse_official_home(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.mphasis.com":
        raise ValueError("Mphasis official careers page redirected outside careers.mphasis.com")
    soup = BeautifulSoup(html, "lxml")
    job_links = [anchor.get("href") for anchor in soup.select('a[title="Job Search"]')]
    if RIPPLEHIRE_JOBS_LINK not in job_links:
        raise ValueError("Mphasis official careers page no longer links directly to its RippleHire job search")
    careers_routes = [anchor.get("href") for anchor in soup.select('a[href="/home/mphasis-careers.html"]')]
    if not careers_routes:
        raise ValueError("Mphasis official careers page no longer exposes its careers listing route")
    match = re.search(r'var\s+rhLink\s*=\s*"([^"]+)"', html)
    if not match:
        raise ValueError("Mphasis official careers page no longer exposes the RippleHire candidate link")
    rh_link = _decode_js_url(match.group(1))
    candidate = parse_candidate_listing_url(rh_link)
    return {
        "url": response_url,
        "job_search_links": sorted(set(job_links)),
        "careers_routes": sorted(set(careers_routes)),
        "rh_link": rh_link,
        "rh_token": candidate["token"],
        "rh_source": candidate["source"],
    }


def parse_candidate_listing_url(value: str) -> dict:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != "mphasis.ripplehire.com"
            or parsed.path.rstrip("/") not in ("/candidate", "/candidate/careers")):
        raise ValueError("Mphasis candidate listing URL is not the inspected RippleHire route")
    token = (query.get("token") or [""])[0]
    source = (query.get("source") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token or ""):
        raise ValueError("Mphasis candidate listing URL is missing its company token")
    if source != SOURCE_FLAG:
        raise ValueError("Mphasis candidate listing URL source is no longer CAREERSITE")
    base = urlunsplit(("https", "mphasis.ripplehire.com", CANDIDATE_PATH,
                       urlencode({"token": token, "source": source}), ""))
    return {
        "token": token,
        "source": source,
        "base_url": base,
        "listing_url": base + "#list",
        "request_url": value,
        "redirect_final_url": value,
    }


def _jtext(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ("true", "y", "1")


def _listing_result(rows: list[dict], start: int, max_size: int, total: int,
                    error_status: str, requested_page: int, pagesize: int,
                    expected_total: int | None, extra_issues: list[str] | None = None) -> dict:
    issues = list(extra_issues or [])
    if error_status.lower() not in ("", "success"):
        issues.append(f"mphasis_search_error_status:{error_status}")
    if start != requested_page * pagesize:
        issues.append(f"mphasis_search_start_index_mismatch:{start}")
    if max_size != pagesize:
        issues.append(f"mphasis_search_page_size_mismatch:{max_size}")
    if total < 0:
        issues.append("mphasis_search_total_negative")
    expected_rows = min(pagesize, max(0, total - start))
    if len(rows) != expected_rows:
        issues.append(f"mphasis_search_page_row_count_mismatch:{len(rows)}!={expected_rows}")
    if expected_total is not None and total != expected_total:
        issues.append(f"mphasis_search_total_changed:{expected_total}->{total}")
    ids = [_jtext(row.get("jobSeq")) for row in rows]
    if any(not identity for identity in ids):
        issues.append("mphasis_search_row_missing_job_seq")
    if len(ids) != len(set(ids)):
        issues.append("mphasis_search_duplicate_job_seq_on_page")
    return {
        "page": requested_page,
        "start": start,
        "max_size": max_size,
        "total": total,
        "rows": rows,
        "job_ids": ids,
        "error_status": error_status,
        "issues": issues,
    }


def parse_listing_page(content: bytes | str, requested_page: int, pagesize: int = PAGE_SIZE,
                       expected_total: int | None = None) -> dict:
    soup = BeautifulSoup(_decode(content), "lxml-xml")
    root = soup.find("JobPageVO")
    if root is None:
        raise ValueError("Mphasis candidate job search response is missing JobPageVO")
    try:
        start = int(_field(root, "startJobIndex"))
        max_size = int(_field(root, "maxJobSize"))
        total = int(_field(root, "totalJobCount"))
    except ValueError as exc:
        raise ValueError("Mphasis candidate job search pagination fields changed") from exc
    container = root.find("jobVoList")
    items = container.find_all("jobVoList", recursive=False) if container is not None else []
    rows = [{child.name: child.get_text().strip() for child in item.find_all(recursive=False)}
            for item in items]
    return _listing_result(rows, start, max_size, total, _field(root, "errorStatus"),
                           requested_page, pagesize, expected_total)


def parse_listing_json(value, requested_page: int, pagesize: int = PAGE_SIZE,
                       expected_total: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Mphasis candidate job search JSON is not an object")
    rows = value.get("jobVoList")
    if not isinstance(rows, list):
        raise ValueError("Mphasis candidate job search JSON is missing jobVoList")
    valid_rows = [dict(row) for row in rows if isinstance(row, dict)]
    issues = [] if len(valid_rows) == len(rows) else [f"mphasis_search_non_object_rows:{len(rows) - len(valid_rows)}"]
    try:
        start = int(value.get("startJobIndex"))
        max_size = int(value.get("maxJobSize"))
        total = int(value.get("totalJobCount"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Mphasis candidate job search JSON pagination fields changed") from exc
    return _listing_result(valid_rows, start, max_size, total, _jtext(value.get("errorStatus")),
                           requested_page, pagesize, expected_total, issues)


def description_sections(value: str) -> list[dict]:
    """Section the visible description text by its employer-authored headings."""
    soup = BeautifulSoup(f'<div data-mphasis-body="true">{value or ""}</div>', "lxml")
    root = soup.select_one("[data-mphasis-body]")
    if root is None:
        return []
    full_text = normalized_text(text_html(str(root)))
    headings = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
        heading = ""
        if node.name != "p":
            heading = normalized_title(node.get_text(" ", strip=True))
        else:
            strong = node.find("strong", recursive=False)
            full = normalized_title(node.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full and len(full) <= 120:
                heading = full
        if heading and (not headings or headings[-1] != heading):
            headings.append(heading)
    sections, cursor = [], 0
    for index, heading in enumerate(headings):
        position = full_text.find(heading, cursor)
        if position < 0:
            continue
        body_start = position + len(heading)
        next_position = len(full_text)
        if index + 1 < len(headings):
            found = full_text.find(headings[index + 1], body_start)
            if found >= 0:
                next_position = found
        sections.append({
            "heading": heading,
            "html": "",
            "text": full_text[body_start:next_position].strip(),
        })
        cursor = next_position
    return sections


def _detail_result(job_fields: dict, company_fields: dict, root_fields: dict,
                    job_desc_html: str, skills_html: str, other_html: str,
                    expected_seq: str | None = None) -> dict:
    job_seq = _jtext(job_fields.get("jobSeq"))
    title = normalized_title(_jtext(job_fields.get("jobTitle")))
    job_code = (_jtext(job_fields.get("jobCode")) or _jtext(job_fields.get("jobId")) or job_seq).strip()
    job_status = _jtext(job_fields.get("jobStatus"))
    enable_link = _jtext(root_fields.get("enableLink"))
    linked = not _truthy(root_fields.get("linkDeactivated"))
    non_er_linked = not _truthy(root_fields.get("nonErLinkDeactivated"))
    apply_allowed = (job_status.upper() == "ACTIVE" and enable_link.upper() == "Y"
                     and linked and non_er_linked)

    job_desc_html = job_desc_html or ""
    skills_html = skills_html or ""
    other_html = other_html or ""
    description_parts = ["<h3>Job description</h3>", f'<div class="description">{job_desc_html}</div>']
    if skills_html.strip():
        description_parts += ["<h3>Skills</h3>", f'<div class="skills">{skills_html}</div>']
    if other_html.strip():
        description_parts += ["<h3>Other details</h3>", f'<div class="otherdetails">{other_html}</div>']
    description_html = "\n".join(description_parts)
    description_text = normalized_text(text_html(description_html))
    other_text = normalized_text(text_html(other_html))

    sections = description_sections(job_desc_html)
    responsibilities = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                      for token in ("responsibilit", "duties", "what you will do", "what you'll do"))
    ] or None
    qualifications = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                      for token in ("qualif", "requirement", "required skills", "skills",
                                                    "education", "who you are", "what you will need",
                                                    "what you'll need"))
    ]
    if skills_html.strip() and not any("skill" in (section["heading"] or "").lower()
                                       for section in qualifications):
        qualifications.append({
            "heading": "Skills",
            "html": skills_html,
            "text": normalized_text(text_html(skills_html)),
        })
    qualifications = qualifications or None

    locations_value = _jtext(job_fields.get("locations")) or _jtext(job_fields.get("jobLocation"))
    locations = [locations_value] if locations_value else []
    currency = _jtext(company_fields.get("currencyCode"))
    salary = parse_salary(other_text, description_text, currency)
    date_posted_raw = _jtext(job_fields.get("jobPostingDate"))
    valid_through_raw = _jtext(job_fields.get("hiringDeadline"))
    try:
        min_months = int(_jtext(job_fields.get("jobMinExp"))) if _jtext(job_fields.get("jobMinExp")) else None
    except ValueError:
        min_months = None
    try:
        max_months = int(_jtext(job_fields.get("jobMaxExp"))) if _jtext(job_fields.get("jobMaxExp")) else None
    except ValueError:
        max_months = None
    issues = []
    if expected_seq is not None and job_seq != expected_seq:
        issues.append(f"mphasis_detail_job_seq_mismatch:{expected_seq}!={job_seq}")
    if not title:
        issues.append("mphasis_detail_missing_title")
    if not job_code:
        issues.append("mphasis_detail_missing_job_code")
    if not description_text or len(description_text) < 100:
        issues.append("mphasis_detail_description_empty_or_short")
    if job_status.upper() != "ACTIVE":
        issues.append(f"mphasis_posting_status:{job_status or 'UNKNOWN'}")
    if not apply_allowed:
        issues.append("mphasis_apply_link_not_enabled")
    employer = {
        "name": _jtext(company_fields.get("companyName")),
        "legal_entity_code": _jtext(company_fields.get("companyCd")),
        "legal_entity": _jtext(company_fields.get("multiCompanyName")),
        "parent_code": _jtext(company_fields.get("parentCode")),
        "currency_code": currency,
    }
    return {
        "job_seq": job_seq,
        "title": title,
        "job_code": job_code,
        "job_status": job_status,
        "enable_link": enable_link,
        "apply_allowed": apply_allowed,
        "date_posted": parse_date(date_posted_raw),
        "date_posted_raw": date_posted_raw,
        "valid_through": parse_date(valid_through_raw),
        "valid_through_raw": valid_through_raw,
        "locations": locations,
        "department": _jtext(job_fields.get("bussinessUnit")) or None,
        "employment_type": _jtext(job_fields.get("jobTypeCustom3")) or None,
        "experience": {
            "text": _jtext(job_fields.get("jobReqExp")) or None,
            "min_months": min_months,
            "max_months": max_months,
        },
        "salary": salary,
        "employer": employer,
        "description_html": description_html,
        "description_text": description_text,
        "job_desc_html": job_desc_html,
        "skills_html": skills_html,
        "other_details_html": other_html,
        "other_details_text": other_text,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "sections": sections,
        "job_fields": job_fields,
        "company_fields": company_fields,
        "root_fields": root_fields,
        "issues": issues,
    }


def parse_detail_xml(content: bytes | str, expected_seq: str | None = None) -> dict:
    raw_text = _decode(content)
    soup = BeautifulSoup(raw_text, "lxml-xml")
    job_node = soup.find("jobVO")
    company_node = soup.find("companyVO")
    if job_node is None or company_node is None:
        raise ValueError("Mphasis candidate job detail response is missing jobVO or companyVO")
    job_fields = {child.name: child.get_text().strip() for child in job_node.find_all(recursive=False)}
    company_fields = {child.name: child.get_text().strip() for child in company_node.find_all(recursive=False)}
    publish_node = job_node.find("publishDetails")
    if publish_node is not None:
        job_fields["publishDetails"] = {
            child.name: child.get_text().strip() for child in publish_node.find_all(recursive=False)
        }
    root_fields = {tag: _field(soup, tag) for tag in
                   ("enableLink", "linkDeactivated", "nonErLinkDeactivated", "jobSeq", "jobCount", "token")}
    return _detail_result(
        job_fields,
        company_fields,
        root_fields,
        clean_fragment(_inner_tag(raw_text, "jobDesc")),
        clean_fragment(_inner_tag(raw_text, "jobSkills")),
        clean_fragment(_inner_tag(raw_text, "otherDetails")),
        expected_seq,
    )


def parse_detail_json(value, expected_seq: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Mphasis candidate job detail JSON is not an object")
    job_fields = value.get("jobVO")
    company_fields = value.get("companyVO")
    if not isinstance(job_fields, dict) or not isinstance(company_fields, dict):
        raise ValueError("Mphasis candidate job detail JSON is missing jobVO or companyVO")
    root_fields = {key: item for key, item in value.items() if key not in ("jobVO", "companyVO")}
    return _detail_result(
        job_fields,
        company_fields,
        root_fields,
        _jtext(job_fields.get("jobDesc")),
        _jtext(job_fields.get("jobSkills")),
        _jtext(job_fields.get("otherDetails")),
        expected_seq,
    )


class Mphasis(Adapter):
    """Read Mphasis's published RippleHire candidate listing and its exact job details."""

    VERIFICATION_REVISION = "mphasis.ripplehire-candidate-api.1"

    async def _official_journey(self) -> dict:
        started = time.perf_counter()
        home = await self.http.request("GET", OFFICIAL_ENTRY)
        home_value = parse_official_home(home.text, str(home.url))
        home_value.update(
            status=home.status_code,
            final_url=str(home.url),
            response_sha256=sha256(home.content).hexdigest(),
            response_bytes=len(home.content),
            redirects=[str(item.url) for item in home.history],
        )
        redirect = await self.http.request("GET", OFFICIAL_LISTING_REDIRECT)
        candidate = parse_candidate_listing_url(str(redirect.url))
        if candidate["token"] != home_value["rh_token"]:
            raise ValueError("Mphasis footprint listing token differs from the official homepage widget token")
        candidate["redirects"] = [str(item.url) for item in redirect.history]
        candidate["status"] = redirect.status_code
        candidate["response_sha256"] = sha256(redirect.content).hexdigest()
        search = await self.http.request("GET", RIPPLEHIRE_JOBS_LINK)
        search_candidate = parse_candidate_listing_url(str(search.url))
        if search_candidate["token"] != candidate["token"]:
            raise ValueError("Mphasis Job Search link and careers route no longer expose one company token")
        return {
            "home": home_value,
            "listing_redirect": {
                "url": OFFICIAL_LISTING_REDIRECT,
                "final_url": str(redirect.url),
                "status": redirect.status_code,
                "redirects": candidate.pop("redirects"),
                "response_sha256": candidate.pop("response_sha256"),
            },
            "job_search_link": {
                "request_url": RIPPLEHIRE_JOBS_LINK,
                "final_url": str(search.url),
                "status": search.status_code,
                "redirects": [str(item.url) for item in search.history],
                "response_sha256": sha256(search.content).hexdigest(),
            },
            "candidate": candidate,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _listing_page(self, page: int, token: str, source: str,
                            listing_url: str, expected_total: int | None = None) -> dict:
        payload = {"page": page, "search": "*:*", "token": token,
                   "source": source, "pagesize": PAGE_SIZE}
        started = time.perf_counter()
        response = await self.http.request(
            "POST", SEARCH_API,
            data={"careerSiteUrlParams": json.dumps(payload, separators=(",", ":")), "lang": "en"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": listing_url,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        seconds = max(0.0, time.perf_counter() - started)
        if "json" in (response.headers.get("content-type") or "").lower():
            try:
                parsed = parse_listing_json(response.json(), page, PAGE_SIZE, expected_total)
            except (ValueError, TypeError):
                parsed = parse_listing_page(response.content, page, PAGE_SIZE, expected_total)
        else:
            parsed = parse_listing_page(response.content, page, PAGE_SIZE, expected_total)
        parsed.update(
            request_url=str(response.request.url),
            final_url=str(response.url),
            status=response.status_code,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            request_seconds=seconds,
            request_body={"careerSiteUrlParams": payload, "lang": "en"},
        )
        return parsed

    async def _collect_listing(self, token: str, source: str, listing_url: str,
                               expected_total: int | None = None) -> dict:
        pages, rows, issues, seen = [], [], [], set()
        total = expected_total
        page = 0
        while True:
            if not self.page_guard():
                issues.append("mphasis_listing_page_budget_exhausted")
                break
            value = await self._listing_page(page, token, source, listing_url, total)
            pages.append(value)
            issues.extend(value["issues"])
            if total is None:
                total = value["total"]
            for row in value["rows"]:
                identity = row.get("jobSeq", "")
                if identity in seen:
                    issues.append(f"mphasis_duplicate_job_seq_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            if not value["rows"]:
                break
            if total is not None and len(rows) >= total:
                break
            if page > 10000:
                issues.append("mphasis_listing_page_budget_exhausted")
                break
            page += 1
        if total is None:
            raise ValueError("Mphasis listing did not return a total job count")
        if len(rows) != total:
            issues.append(f"mphasis_listing_total_mismatch:{len(rows)}!={total}")
        return {
            "rows": rows,
            "job_ids": [row.get("jobSeq", "") for row in rows],
            "total": total,
            "pages": pages,
            "page_count": len(pages),
            "issues": list(dict.fromkeys(issues)),
            "seconds": sum(page["request_seconds"] for page in pages),
        }

    def _listing_job(self, row: dict, candidate: dict):
        identity = row.get("jobSeq", "")
        code = row.get("jobCode") or row.get("jobId") or identity
        locations_value = row.get("locations") or row.get("jobLocation") or ""
        return self.job(
            {"listing": dict(row), "listing_page": row.get("_page"),
             "ripplehire_job_seq": identity},
            title=normalized_title(row.get("jobTitle")) or f"Job {identity}",
            url=candidate["base_url"] + f"#detail/job/{identity}",
            job_id=code,
            requisition_id=code,
            locations=[locations_value] if locations_value else [],
            employer="Mphasis",
        )

    def _detail_job(self, listing_job, detail: dict, response, candidate: dict):
        identity = detail["job_seq"]
        job = self.job(
            {
                "listing": dict(listing_job.raw.get("listing") or {}),
                "ripplehire_job_seq": identity,
                "detail": detail["job_fields"],
                "company": detail["company_fields"],
                "detail_response": {
                    "url": str(response.request.url),
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "response_sha256": sha256(response.content).hexdigest(),
                    "response_bytes": len(response.content),
                },
                "job_description_html": detail["job_desc_html"],
                "job_skills_html": detail["skills_html"],
                "other_details_html": detail["other_details_html"],
                "other_details_text": detail["other_details_text"],
                "description_sections": detail["sections"],
                "listing_title": listing_job.title,
                "apply_route": candidate["base_url"] + f"#apply/job/{identity}" if detail["apply_allowed"] else None,
            },
            title=detail["title"] or listing_job.title,
            url=listing_job.url,
            job_id=detail["job_code"],
            requisition_id=detail["job_code"],
            description=detail["description_html"],
            method="ripplehire.candidatejobdetail.jobDesc+jobSkills+otherDetails",
            description_url=str(response.request.url),
            apply_url=candidate["base_url"] + f"#apply/job/{identity}" if detail["apply_allowed"] else None,
            locations=detail["locations"],
            department=detail["department"],
            employment_type=detail["employment_type"],
            workplace_type=None,
            date_posted=detail["date_posted"],
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            valid_through=detail["valid_through"],
            salary=detail["salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            experience=detail["experience"],
            employer=detail["employer"],
            language="en",
            warnings=list(detail["issues"]),
        )
        job.description_text = detail["description_text"]
        if listing_job.title and normalized_title(listing_job.title) != normalized_title(detail["title"]):
            job.warnings.append("mphasis_listing_detail_title_conflict")
        if detail["job_status"].upper() != "ACTIVE":
            job.warnings.append("mphasis_closed_posting")
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def _detail(self, job):
        seq = job.raw["ripplehire_job_seq"]
        candidate = self.result.metadata["candidate"]
        started = time.perf_counter()
        response = await self.http.request(
            "GET", DETAIL_API,
            params={"token": candidate["token"], "jobSeq": seq,
                    "source": candidate["source"], "lang": "en"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": job.url,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        seconds = max(0.0, time.perf_counter() - started)
        if "json" in (response.headers.get("content-type") or "").lower():
            try:
                parsed = parse_detail_json(response.json(), expected_seq=seq)
            except (ValueError, TypeError):
                parsed = parse_detail_xml(response.content, expected_seq=seq)
        else:
            parsed = parse_detail_xml(response.content, expected_seq=seq)
        parsed["request_seconds"] = seconds
        return self._detail_job(job, parsed, response, candidate)

    async def run(self):
        journey_started = time.perf_counter()
        journey = await self._official_journey()
        self.result.metadata["official_journey"] = journey
        candidate = journey["candidate"]
        self.result.metadata["candidate"] = candidate
        self.result.timings["official_journey_seconds"] = max(0.0, time.perf_counter() - journey_started)

        listing_started = time.perf_counter()
        listing = await self._collect_listing(candidate["token"], candidate["source"], candidate["listing_url"])
        self.result.timings["initial_listing_seconds"] = max(0.0, time.perf_counter() - listing_started)
        self.result.expected_total = listing["total"]
        self.result.metadata["listing"] = {
            "url": candidate["listing_url"],
            "total": listing["total"],
            "page_count": listing["page_count"],
            "page_size": PAGE_SIZE,
            "pages": [
                {"page": page["page"], "start": page["start"], "rows": len(page["rows"]),
                 "job_ids": page["job_ids"], "seconds": page["request_seconds"],
                 "response_sha256": page["response_sha256"]}
                for page in listing["pages"]
            ],
            "issues": listing["issues"],
        }
        for issue in listing["issues"]:
            if "total_mismatch" in issue or "duplicate" in issue or "budget" in issue:
                self.result.errors.append(issue)
            else:
                self.result.warnings.append(issue)

        for page in listing["pages"]:
            for row in page["rows"]:
                row = dict(row)
                row["_page"] = page["page"]
                self.add(self._listing_job(row, candidate))

        detail_started = time.perf_counter()
        await self.hydrate(self.result.jobs, self._detail)
        self.result.timings["detail_phase_seconds"] = max(
            self.result.timings.get("detail_phase_seconds", 0.0),
            time.perf_counter() - detail_started,
        )

        recheck_started = time.perf_counter()
        final_listing = await self._collect_listing(
            candidate["token"], candidate["source"], candidate["listing_url"],
            expected_total=self.result.expected_total,
        )
        final_seconds = max(0.0, time.perf_counter() - recheck_started)
        self.result.timings["final_membership_recheck_seconds"] = final_seconds
        initial_ids = listing["job_ids"]
        final_ids = final_listing["job_ids"]
        matches = initial_ids == final_ids and final_listing["total"] == listing["total"]
        self.result.metadata["final_membership_recheck"] = {
            "total": final_listing["total"],
            "page_count": final_listing["page_count"],
            "job_ids": final_ids,
            "matches_initial_membership": matches,
            "issues": final_listing["issues"],
            "seconds": final_seconds,
        }
        if not matches:
            self.result.errors.append("mphasis_listing_membership_changed_during_run")
        for issue in final_listing["issues"]:
            if "total_changed" in issue or "mismatch" in issue:
                self.result.errors.append(issue)

        detail_count = sum(1 for job in self.result.jobs if job.description_method)
        if detail_count != self.result.expected_total:
            self.result.errors.append(f"mphasis_detail_count_mismatch:{detail_count}!={self.result.expected_total}")
        if self.result.jobs and all(job.description_verified for job in self.result.jobs):
            pass
        self.result.listing_complete = (
            not self.result.errors
            and matches
            and len(self.result.jobs) == self.result.expected_total
            and detail_count == self.result.expected_total
        )
        self.result.metadata["scrape"] = {
            "revision": self.VERIFICATION_REVISION,
            "listing_api": SEARCH_API,
            "detail_api": DETAIL_API,
            "page_size": PAGE_SIZE,
            "initial_total": listing["total"],
            "final_total": final_listing["total"],
            "initial_membership": initial_ids,
            "final_membership": final_ids,
            "closed_postings": [
                job.job_id for job in self.result.jobs
                if "mphasis_closed_posting" in job.warnings
            ],
            "title_conflicts": [
                {"job_id": job.job_id, "listing_title": job.raw.get("listing_title"), "detail_title": job.title}
                for job in self.result.jobs
                if "mphasis_listing_detail_title_conflict" in job.warnings
            ],
        }
        self.result.finish()
        return self.result
