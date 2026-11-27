"""Okta's own first-party Drupal careers listing and full job pages.

The company's careers page links to one global ``/company/careers/job-listing/``
view.  That unfiltered response is the membership authority.  It is rendered by
the company itself (no ATS board or guessed feed), exposes department/location
filters, and has no pagination.  Each listing row links to a company-owned
``/company/careers/<slug>/`` page with the full employer-authored description.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://www.okta.com/company/careers/"
LISTING_URL = "https://www.okta.com/company/careers/job-listing/"
HOST = "www.okta.com"
LISTING_PATH = "/company/careers/job-listing/"
JOB_PATH = re.compile(r"^/company/careers/((?:[a-z0-9][a-z0-9-]*/)?[a-z0-9][a-z0-9-]*)/$", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUISITION = re.compile(r"(?<![A-Za-z0-9])(P\d{4,}(?:_\d+)?)(?![A-Za-z0-9])")
RESPONSIBILITY_HEADING = re.compile(
    r"responsibilit|what you.?ll do|what you will do|your impact|key duties|day.to.day|the role",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualification|requirement|what you.?ll bring|what you bring|good fit|"
    r"about you|who you are|skills|you have",
    re.I,
)
SECTION_HEADING = re.compile(r"^[A-Za-z][^.!?]{0,95}:$")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
EMPLOYER_BOILERPLATE_HEADING = "The Okta Experience"
INLINE_BOILERPLATE_PREFIXES = (
    "okta is an equal opportunity employer",
    "all qualified applicants will receive consideration for employment",
    "we also consider for employment qualified applicants",
    "if reasonable accommodation is needed",
    "notice for new york city applicants",
    "okta is rethinking the traditional work environment",
    "each organization is unique in the degree of flexibility",
    "by submitting an application, you agree to the retention",
    "okta is committed to complying with applicable data privacy",
    "find your place at okta today",
)


def normalized_title(value: str | None) -> str:
    """Normalize Unicode composition and whitespace; preserve title wording."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", unescape(value or ""))).strip()


def rendered_text(html: str | None) -> str:
    """Approximate visible inline text and collapse browser whitespace."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?.])", r"\1", value)
    value = re.sub(r":\s+(?=[$£€])", ":", value)
    value = re.sub(r"(?<=[\d$£€])\s*([—–])\s*(?=\$?[\d£€])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _sha256(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _url_is(value: str, *, path: str, allow_query: bool = False) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != HOST:
        return False
    if parsed.path.rstrip("/") != path.rstrip("/"):
        return False
    return allow_query or not parsed.query


def _selected(select: Tag) -> list[dict]:
    return [
        {"value": option.get("value"), "label": normalized_title(option.get_text(" ", strip=True))}
        for option in select.find_all("option")
        if option.has_attr("selected")
    ]


def _options(select: Tag) -> list[dict]:
    return [
        {
            "value": option.get("value"),
            "label": normalized_title(option.get_text(" ", strip=True)),
            "selected": option.has_attr("selected"),
        }
        for option in select.find_all("option")
    ]


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Verify Okta's own careers page and the exact global listing it links to."""
    if not _url_is(response_url, path="/company/careers"):
        raise ValueError("Okta official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Okta" not in title or "Careers" not in title:
        raise ValueError("Okta official careers entry no longer identifies Okta careers")
    links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        if _url_is(target, path=LISTING_PATH):
            links.append({
                "url": target,
                "label": normalized_title(anchor.get_text(" ", strip=True)),
                "class": " ".join(anchor.get("class") or []),
            })
    if not links:
        raise ValueError("Okta official careers page no longer links to its global job listing")
    targets = {row["url"] for row in links}
    if targets != {LISTING_URL}:
        raise ValueError("Okta official careers page links to an unexpected job-listing URL")
    if not any(re.search(r"open|all jobs|view", row["label"], re.I) for row in links):
        raise ValueError("Okta official careers listing links no longer carry a visible jobs label")
    return {"url": response_url, "title": title, "listing_urls": [LISTING_URL],
            "listing_links": links, "listing_link_count": len(links)}


def _parse_listing_filters(soup: BeautifulSoup, *, filtered_department: str | None = None) -> dict:
    form = soup.select_one("form#views-exposed-form-careers-main")
    if form is None:
        raise ValueError("Okta listing's company filter form is missing")
    selects = {select.get("name"): select for select in form.select("select") if select.get("name")}
    if set(selects) != {"department", "location"}:
        raise ValueError("Okta listing's department/location filter controls changed")
    filters = {name: _options(select) for name, select in selects.items()}
    for name, rows in filters.items():
        if not rows or rows[0].get("value") != "All":
            raise ValueError(f"Okta listing {name} filter no longer starts with an unfiltered option")
        if len({row.get("value") for row in rows}) != len(rows):
            raise ValueError(f"Okta listing {name} filter contains duplicate option values")
    selected = {name: _selected(select) for name, select in selects.items()}
    if filtered_department:
        department_selected = selected.get("department") or []
        if [row["value"] for row in department_selected] != [filtered_department]:
            raise ValueError("Okta filtered listing did not select the requested department")
    return {"filters": filters, "selected": selected}


def parse_listing(html: str, response_url: str = LISTING_URL, *,
                  filtered_department: str | None = None,
                  filtered_label: str | None = None) -> dict:
    """Parse an unfiltered or department-filtered Okta careers listing page."""
    if not _url_is(response_url, path=LISTING_PATH, allow_query=True):
        raise ValueError("Okta listing response came from an unexpected URL")
    parsed_url = urlsplit(response_url)
    query = parsed_url.query
    if filtered_department and f"department={filtered_department}" not in query:
        raise ValueError("Okta department-filter response URL does not contain the requested filter")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Job Openings" not in title or "Okta" not in title:
        raise ValueError("Okta job-listing response no longer identifies its Job Openings page")
    controls = _parse_listing_filters(soup, filtered_department=filtered_department)

    view = soup.select_one("div.CareersView__content")
    rows, issues = [], []
    current_department = None
    if view is not None:
        for element in view.find_all(["h3", "div"], recursive=False):
            if element.name == "h3":
                current_department = normalized_title(element.get_text(" ", strip=True)) or None
                continue
            if "views-row" not in (element.get("class") or []):
                continue
            anchor = element.select_one('a[href^="/company/careers/"]')
            location = element.select_one("div.views-field-field-job-location .field-content")
            if anchor is None:
                issues.append(f"okta_listing_row_missing_company_job_link:{len(rows)}")
                continue
            path = anchor.get("href") or ""
            match = JOB_PATH.fullmatch(path)
            if match is None:
                issues.append(f"okta_listing_row_unexpected_path:{len(rows)}")
                continue
            title_text = normalized_title(anchor.get_text(" ", strip=True))
            location_text = normalized_title(location.get_text(" ", strip=True)) if location else ""
            if not title_text:
                issues.append(f"okta_listing_row_missing_title:{match.group(1)}")
            if not location_text:
                issues.append(f"okta_listing_row_missing_location:{match.group(1)}")
            if filtered_department and current_department != filtered_label:
                issues.append(
                    f"okta_filtered_row_wrong_group:{match.group(1)}:{current_department}:{filtered_label}")
            rows.append({
                "index": len(rows),
                "id": match.group(1),
                "slug": match.group(1).rsplit("/", 1)[-1],
                "path": path,
                "url": urljoin(response_url, path),
                "title": title_text,
                "location": location_text,
                "locations": [part.strip() for part in location_text.split(";") if part.strip()],
                "department": current_department,
                "row_html_sha256": _sha256(str(element)),
            })
    elif not filtered_department:
        raise ValueError("Okta unfiltered listing no longer renders its company job rows")

    if filtered_department and not rows and filtered_label:
        # The current Drupal view removes the whole content wrapper when a valid
        # department has no postings.  Keep that as a verified empty filter result.
        issues = [item for item in issues if not item.startswith("okta_filtered_row_wrong_group")]

    seen = {}
    for row in rows:
        if row["path"] in seen:
            issues.append(f"okta_listing_duplicate_path:{row['id']}")
        seen[row["path"]] = row
    pager = [
        normalized_title(node.get_text(" ", strip=True)) or node.get("href", "")
        for node in soup.select(
            "nav.pager, ul.pager, div.pager, li.pager__item, a[rel='next'], a[rel='previous']")
    ]
    pager += [
        anchor.get("href") for anchor in soup.select("a[href]")
        if re.search(r"(?:[?&]page=|/page/)\d+", anchor.get("href", ""))
    ]
    pager = [item for item in pager if item]
    group_counts = {}
    for row in rows:
        key = row["department"] or "null"
        group_counts[key] = group_counts.get(key, 0) + 1
    return {
        "url": response_url,
        "title": title,
        "rows": rows,
        "count": len(rows),
        "job_ids": [row["id"] for row in rows],
        "paths": [row["path"] for row in rows],
        "group_counts": group_counts,
        "issues": issues,
        "pagination_controls": pager,
        "filter_options": controls["filters"],
        "selected_filters": controls["selected"],
        "empty_filter_result": bool(filtered_department and not rows),
    }


def _heading_for(element: Tag) -> str | None:
    if element.name in HEADING_TAGS:
        return normalized_title(element.get_text(" ", strip=True)) or None
    if element.name != "p":
        return None
    text = normalized_title(element.get_text(" ", strip=True))
    if not text or len(text) > 110:
        return None
    strong = element.find("strong")
    if SECTION_HEADING.fullmatch(text):
        return text
    if strong is not None and normalized_title(strong.get_text(" ", strip=True)) == text:
        # Short bold-only paragraphs are used as section labels by the source
        # (for example "The Okta Experience").
        return text
    return None


def description_sections(html: str) -> list[dict]:
    """Keep employer-authored headings and the exact body below each heading."""
    soup = BeautifulSoup(f'<div data-okta-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-okta-body]")
    if root is None:
        return []
    sections, current = [], None
    for child in root.contents:
        if not isinstance(child, Tag):
            continue
        heading = _heading_for(child)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(child)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = rendered_text(section["html"])
        section["html_sha256"] = _sha256(section["html"])
    return sections


def strip_inline_boilerplate(html: str) -> tuple[str, list[dict]]:
    """Remove repeated employer/EEO/privacy paragraphs from the role body.

    Some Okta postings place the standard EEO/privacy block before the final
    salary paragraphs instead of inside the trailing "The Okta Experience"
    block.  Remove only blocks whose visible text starts with one of the fixed
    company-wide boilerplate sentences; keep role content, tags, and salary.
    """
    soup = BeautifulSoup(f'<div data-okta-role="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-okta-role]")
    removed = []
    if root is None:
        return html, removed
    for node in list(root.find_all("p")):
        text = normalized_title(node.get_text(" ", strip=True))
        lower = text.lower()
        if not text or not any(lower.startswith(prefix) for prefix in INLINE_BOILERPLATE_PREFIXES):
            continue
        removed.append({
            "tag": node.name,
            "chars": len(text),
            "text_sha256": _sha256(text),
            "text": text,
        })
        node.decompose()
    for node in list(root.find_all(["em", "strong", "span"])):
        text = normalized_title(node.get_text(" ", strip=True))
        lower = text.lower()
        if (len(text) < 400
                and any(lower.startswith(prefix) for prefix in INLINE_BOILERPLATE_PREFIXES)):
            removed.append({
                "tag": node.name,
                "chars": len(text),
                "text_sha256": _sha256(text),
                "text": text,
            })
            node.decompose()
    return root.decode_contents(), removed


SALARY_TRIGGER = re.compile(
    r"(?i)(?:below is the annual|the(?: annual)?)\s+"
    r"(?P<kind>on target compensation \(ote\)|ote \(on target earning\)|on target earning \(ote\)|base salary|salary|ote)\s+"
    r"range for(?: this position)? for candidates located in\s+"
    r"(?P<basis>[^.]+?)(?:\.|\s+is between:)"
)
SALARY_RANGE = re.compile(
    r"(?P<symbol1>[$£€])\s*(?P<minimum>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:[-–—−]|to)\s*(?P<symbol2>[$£€])?\s*"
    r"(?P<maximum>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<code>USD|CAD|GBP|EUR|PLN)?"
    r"(?:\s*\((?P<label>[^)]{1,80})\))?",
    re.I,
)
SALARY_ZL_RANGE = re.compile(
    r"(?P<minimum>\d{2,3}(?:[ ]\d{3})+)\s*zł\s*(?:[-–—−]|to)\s*"
    r"(?P<maximum>\d{2,3}(?:[ ]\d{3})+)\s*zł\s*(?P<code>PLN)?"
    r"(?:\s*\((?P<label>[^)]{1,80})\))?",
    re.I,
)
SALARY_SINGLE = re.compile(
    r"(?P<symbol>[$£€])\s*(?P<amount>\d[\d,]{2,})\s*"
    r"(?P<code>USD|CAD|GBP|EUR|PLN)?",
    re.I,
)
SYMBOL_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}


def _money(value: str) -> int:
    raw = re.sub(r"[^0-9.,]", "", value)
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", "")
    elif "." in raw:
        parts = raw.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            raw = "".join(parts)
    value = float(raw or "0")
    return int(value) if value.is_integer() else round(value, 2)


def _salary_kind(value: str) -> str:
    lower = normalized_title(value).lower()
    if "ote" in lower or "target compensation" in lower:
        return "ote"
    if "base" in lower:
        return "base_salary"
    return "salary"


def parse_salary(article_text: str) -> list[dict]:
    """Parse explicit published salary/OTE ranges without inventing amounts.

    Okta uses several phrasings and places the actual numbers either in the same
    paragraph or in immediately following spans.  Trigger on the company's own
    salary/OTE sentence, then inspect the text up to the next trigger so two
    separate location paragraphs cannot borrow each other's amounts.
    """
    if not article_text:
        return []
    triggers = list(SALARY_TRIGGER.finditer(article_text))
    ranges, seen = [], set()
    for index, trigger in enumerate(triggers):
        window_end = triggers[index + 1].start() if index + 1 < len(triggers) else len(article_text)
        window = article_text[trigger.end():window_end]
        basis = normalized_title(trigger.group("basis"))
        if basis.lower().startswith("the "):
            basis = basis[4:]
        kind = _salary_kind(trigger.group("kind"))
        matches = []
        for pattern in (SALARY_ZL_RANGE, SALARY_RANGE):
            for match in pattern.finditer(window):
                matches.append((match.start(), match.end(), match, pattern is SALARY_ZL_RANGE))
        matches.sort(key=lambda item: (item[0], -item[1]))
        occupied = []
        for start, end, match, is_zl in matches:
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            minimum = _money(match.group("minimum"))
            maximum = _money(match.group("maximum"))
            if minimum < 1000 or maximum < minimum:
                continue
            code = (match.groupdict().get("code") or "")
            symbol = None if is_zl else match.groupdict().get("symbol1")
            currency = code.upper() if code else SYMBOL_CURRENCY.get(symbol or "", "PLN" if is_zl else None)
            if not currency:
                continue
            key = (kind, basis, minimum, maximum, currency,
                   normalized_title(match.groupdict().get("label")))
            if key in seen:
                continue
            seen.add(key)
            ranges.append({
                "kind": kind,
                "basis": basis,
                "minimum": minimum,
                "maximum": maximum,
                "currency": currency,
                "period": "annual",
                "location_label": normalized_title(match.groupdict().get("label")) or None,
                "source_text": normalized_title(trigger.group(0) + window)[:1200],
            })
        if occupied:
            continue
        singles = list(SALARY_SINGLE.finditer(window))
        if len(singles) == 1:
            match = singles[0]
            amount = _money(match.group("amount"))
            code = (match.group("code") or "").upper()
            currency = code or SYMBOL_CURRENCY.get(match.group("symbol"))
            if amount >= 1000 and currency:
                key = (kind, basis, amount, None, currency, None)
                if key not in seen:
                    seen.add(key)
                    ranges.append({
                        "kind": kind,
                        "basis": basis,
                        "minimum": amount,
                        "maximum": None,
                        "currency": currency,
                        "period": "annual",
                        "location_label": None,
                        "source_text": normalized_title(trigger.group(0) + window)[:1200],
                    })
    return ranges

def _job_posting_script(soup: BeautifulSoup) -> dict:
    postings = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            postings.append(value)
    if len(postings) != 1:
        raise ValueError(f"Okta job page has {len(postings)} JobPosting JSON-LD records, expected 1")
    return postings[0]


def parse_detail(html: str, response_url: str, expected_row: dict) -> dict:
    """Parse one company-owned Okta job page and verify the listing identity."""
    parsed = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != HOST or match is None:
        raise ValueError("Okta job detail response came from an unexpected URL")
    if match.group(1).lower() != expected_row["id"].lower():
        raise ValueError("Okta job detail URL identity does not match the listing row")
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 is None:
        raise ValueError("Okta job detail page has no job title heading")
    title = normalized_title(h1.get_text(" ", strip=True))
    if not title:
        raise ValueError("Okta job detail title is empty")
    posting = _job_posting_script(soup)
    posting_title = normalized_title(posting.get("title"))
    if posting_title and posting_title != title:
        raise ValueError("Okta JSON-LD title does not match the visible job title")
    location_node = h1.find_next_sibling("div", class_="mb-4")
    detail_location = normalized_title(location_node.get_text(" ", strip=True)) if location_node else ""
    articles = soup.select("article.Job__content")
    if len(articles) != 1:
        raise ValueError(f"Okta job page has {len(articles)} full job bodies, expected 1")
    article = articles[0]
    article_html = article.decode_contents()
    article_text = rendered_text(article_html)
    if len(article_text) < 100:
        raise ValueError("Okta job page full body is empty or too short")
    sections = description_sections(article_html)
    requisitions = list(dict.fromkeys(REQUISITION.findall(article_text)))
    industry = normalized_title(posting.get("industry"))
    employment_types = posting.get("employmentType")
    if isinstance(employment_types, str):
        employment_types = [employment_types]
    if not isinstance(employment_types, list):
        employment_types = []
    employment_types = [normalized_title(value) for value in employment_types if normalized_title(value)]

    json_locations = posting.get("jobLocation")
    if isinstance(json_locations, dict):
        json_locations = [json_locations]
    if not isinstance(json_locations, list):
        json_locations = []
    addresses = [
        normalized_title(item.get("address"))
        for item in json_locations
        if isinstance(item, dict) and normalized_title(item.get("address"))
    ]
    detail_parts = [part.strip() for part in detail_location.split(";") if part.strip()]
    listing_parts = list(expected_row.get("locations") or [])
    if not detail_parts and addresses:
        detail_parts = [part.strip() for part in addresses[0].split(";") if part.strip()]
    location_verified = bool(detail_parts and listing_parts and detail_parts == listing_parts)
    title_verified = title == normalized_title(expected_row.get("title"))
    posting_description = posting.get("description")
    posting_text = rendered_text(posting_description) if isinstance(posting_description, str) else ""

    # Every current Okta posting appends the same "The Okta Experience" /
    # equal-opportunity employer block inside article.Job__content.  It is
    # repeated company template text, not role content.  Keep it separately so
    # the stored description stops at the role-specific body.
    article_soup = BeautifulSoup(
        f'<div data-okta-article="true">{article_html}</div>', "lxml")
    article_root = article_soup.select_one("[data-okta-article]")
    children = list(article_root.contents) if article_root is not None else []
    heading_index = next(
        (index for index, child in enumerate(children)
         if isinstance(child, Tag) and _heading_for(child) == EMPLOYER_BOILERPLATE_HEADING),
        None,
    )
    if heading_index is not None:
        role_html = "".join(str(child) for child in children[:heading_index])
        boilerplate_html = "".join(str(child) for child in children[heading_index:])
    else:
        role_html, boilerplate_html = article_html, ""
    role_html, inline_boilerplate = strip_inline_boilerplate(role_html)
    role_text = rendered_text(role_html)
    if len(role_text) < 100:
        raise ValueError("Okta job page role-specific body is empty or too short")
    full_article_text = rendered_text(article_html)
    boilerplate_text = rendered_text(boilerplate_html)
    boilerplate = ({
        "heading": EMPLOYER_BOILERPLATE_HEADING,
        "chars": len(boilerplate_text),
        "text_sha256": _sha256(boilerplate_text),
        "text": boilerplate_text,
    } if boilerplate_html else None)
    sections = description_sections(role_html)
    requisitions = list(dict.fromkeys(REQUISITION.findall(full_article_text)))
    posting_prefix = re.sub(r"\s+", "", posting_text)[:240]
    truncated = bool(
        posting_text
        and len(posting_text) < len(role_text) - 200
        and re.sub(r"\s+", "", role_text).startswith(posting_prefix)
    )
    apply_form = soup.select_one("form#okta-careers-job-form")
    responsibilities = [
        section for section in sections
        if section["heading"] and RESPONSIBILITY_HEADING.search(section["heading"])
    ]
    qualifications = [
        section for section in sections
        if section["heading"] and QUALIFICATION_HEADING.search(section["heading"])
        and "the okta experience" not in section["heading"].lower()
    ]
    return {
        "url": response_url,
        "title": title,
        "h1": title,
        "title_matches_listing": title_verified,
        "detail_location": detail_location,
        "detail_locations": detail_parts,
        "listing_locations": listing_parts,
        "location_matches_listing": location_verified,
        "article_html": role_html,
        "article_text": role_text,
        "article_sha256": _sha256(role_text),
        "article_chars": len(role_text),
        "full_article_chars": len(full_article_text),
        "full_article_sha256": _sha256(full_article_text),
        "employer_boilerplate": boilerplate,
        "inline_boilerplate": inline_boilerplate,
        "sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "requisitions": requisitions,
        "requisition_id": requisitions[0] if requisitions else None,
        "industry": industry or None,
        "employment_types": employment_types,
        "employer": normalized_title((posting.get("hiringOrganization") or {}).get("name"))
        if isinstance(posting.get("hiringOrganization"), dict) else None,
        "date_posted_raw": posting.get("datePosted"),
        "date_updated_raw": posting.get("dateModified"),
        "valid_through_raw": posting.get("validThrough"),
        "json_locations": json_locations,
        "salary": parse_salary(role_text) or None,
        "apply_form_action": apply_form.get("action") if apply_form else None,
        "posting_description_is_truncated": truncated,
        "posting_keys": sorted(posting.keys()),
        "issues": [
            *(["okta_detail_title_changed_from_listing"] if not title_verified else []),
            *(["okta_detail_location_changed_from_listing"] if not location_verified else []),
        ],
    }


class Okta(Adapter):
    """Collect every public role in Okta's own global careers listing."""

    VERIFICATION_REVISION = "okta.drupal-careers-listing.1"

    async def _text(self, url: str):
        started = time.perf_counter()
        response = await self.http.get(url)
        elapsed = max(0.0, time.perf_counter() - started)
        return response, elapsed

    def _request_meta(self, response, elapsed: float) -> dict:
        return {
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "redirects": [str(item.url) for item in response.history],
            "content_type": response.headers.get("content-type"),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "request_elapsed_seconds": round(elapsed, 6),
        }

    def _make_job(self, row: dict):
        identity = row["id"]
        department = row.get("department")
        return self.job(
            {"listing": dict(row)},
            title=row["title"],
            url=row["url"],
            job_id=identity,
            description="",
            description_url=row["url"],
            method="company listing row; full description hydration pending",
            locations=list(row.get("locations") or []),
            department=department,
            employer="Okta, Inc.",
            apply_url=row["url"],
        )

    async def _detail(self, job):
        response, elapsed = await self._text(job.url)
        canonical_meta = {
            **self._request_meta(response, elapsed),
            "source_kind": "official_company_job_detail_canonical_listing_url",
        }
        fallback_used = False
        fallback_url = None
        fallback_meta = None
        canonical_path = urlsplit(str(response.url)).path.rstrip("/")
        expected_path = urlsplit(job.url).path.rstrip("/")
        if canonical_path == "/company/careers" and expected_path != "/company/careers":
            # The listing currently links two exact job paths whose canonical
            # request 301s to the careers hub while the same Drupal node still
            # serves the exact role page with a neutral query string.  Retry the
            # same path, verify the final path and source title/location, and keep
            # the canonical listing URL in job.url.
            fallback_used = True
            fallback_url = job.url + "?source=careers"
            try:
                fallback_response, fallback_seconds = await self._text(fallback_url)
                if urlsplit(str(fallback_response.url)).path.rstrip("/") != expected_path:
                    raise ValueError(
                        "same-job query retry did not return the exact listing row path")
                detail = parse_detail(
                    fallback_response.text, str(fallback_response.url), job.raw["listing"])
                fallback_meta = {
                    **self._request_meta(fallback_response, fallback_seconds),
                    "source_kind": "official_company_job_detail_same_job_query_retry",
                }
                response, elapsed = fallback_response, fallback_seconds
                self.result.metadata.setdefault("workarounds", []).append({
                    "job_id": job.job_id,
                    "canonical_url": job.url,
                    "canonical_final_url": canonical_meta["final_url"],
                    "canonical_status": canonical_meta["status"],
                    "canonical_redirects": canonical_meta["redirects"],
                    "retry_url": fallback_url,
                    "retry_verified_exact_path": True,
                    "reason": "canonical detail URL 301s to the careers hub; same job path with a neutral query serves the exact company job page",
                })
                job.warnings.append("canonical_detail_link_redirected_to_company_hub:same_job_query_retry_used")
            except Exception as exc:
                raise ValueError(
                    "Okta canonical detail link redirected to the careers hub and the "
                    f"same-job query retry failed: {type(exc).__name__}: {exc}") from exc
        else:
            detail = parse_detail(response.text, str(response.url), job.raw["listing"])
        for issue in detail["issues"]:
            job.warnings.append(issue)
            self.result.errors.append(f"{issue}: {job.url}")
        department = job.raw["listing"].get("department") or detail.get("industry")
        job.title = detail["title"]
        job.department = department
        job.locations = detail["detail_locations"] or job.locations
        job.employment_type = (
            detail["employment_types"][0] if len(detail["employment_types"]) == 1
            else detail["employment_types"] or None
        )
        job.employer = detail["employer"] or job.employer
        job.date_posted = detail["date_posted_raw"] if (
            isinstance(detail["date_posted_raw"], str)
            and ISO_DATE.fullmatch(detail["date_posted_raw"])
        ) else None
        job.date_posted_raw = detail["date_posted_raw"]
        job.date_updated = detail["date_updated_raw"] if (
            isinstance(detail["date_updated_raw"], str)
            and ISO_DATE.fullmatch(detail["date_updated_raw"])
        ) else None
        job.valid_through = detail["valid_through_raw"] if (
            isinstance(detail["valid_through_raw"], str)
            and ISO_DATE.fullmatch(detail["valid_through_raw"])
        ) else None
        job.requisition_id = detail["requisition_id"]
        job.salary = detail["salary"]
        job.qualifications = detail["qualifications"]
        job.responsibilities = detail["responsibilities"]
        job.apply_url = detail["url"]
        job.description_html = detail["article_html"]
        job.description_text = detail["article_text"]
        job.description_source_url = detail["url"]
        job.description_method = (
            "exact company-owned article.Job__content on the official Okta job page "
            "(same-job neutral-query retry after the canonical listing link 301ed to the careers hub)"
            if fallback_used else
            "exact company-owned article.Job__content on the official Okta job page"
        )
        job.raw["detail"] = {
            key: value for key, value in detail.items()
            if key not in {"article_html", "article_text"}
        }
        job.raw["detail"]["request"] = {
            **self._request_meta(response, elapsed),
            "source_kind": "official_company_job_detail",
            "canonical_listing_url": job.url,
            "canonical_request": canonical_meta,
            "fallback_used": fallback_used,
            "fallback_url": fallback_url,
            "fallback_request": fallback_meta,
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        entry_response, entry_seconds = await self._text(OFFICIAL_ENTRY)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        self.result.metadata["official_entry"] = {
            **entry,
            **self._request_meta(entry_response, entry_seconds),
            "request_url": OFFICIAL_ENTRY,
        }

        initial_response, initial_seconds = await self._text(LISTING_URL)
        initial = parse_listing(initial_response.text, str(initial_response.url))
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
        self.result.expected_total = initial["count"]
        self.result.pages = 1
        self.result.metadata["initial_listing"] = {
            **initial,
            **self._request_meta(initial_response, initial_seconds),
            "request_url": LISTING_URL,
            "source_kind": "official_company_listing_unfiltered",
        }
        if initial["pagination_controls"]:
            self.result.errors.append("okta_listing_pagination_present")
        if not initial["count"]:
            self.result.errors.append("okta_listing_has_no_company_jobs")

        department_checks = {}
        department_options = [
            option for option in initial["filter_options"]["department"]
            if option.get("value") not in (None, "All")
        ]
        initial_by_id = {row["id"]: row for row in initial["rows"]}
        initial_sequence = [row["path"] for row in initial["rows"]]
        filter_started = time.perf_counter()
        for option in department_options:
            label = option["label"]
            value = option["value"]
            filtered_url = f"{LISTING_URL}?department={value}"
            try:
                response, seconds = await self._text(filtered_url)
                filtered = parse_listing(
                    response.text, str(response.url),
                    filtered_department=value, filtered_label=label,
                )
            except Exception as exc:
                self.result.errors.append(f"okta_department_filter_failed:{label}:{type(exc).__name__}:{exc}")
                self.result.metadata.setdefault("failed_sources", []).append({
                    "url": filtered_url, "reason": f"{type(exc).__name__}: {exc}",
                })
                department_checks[label] = {
                    "value": value, "url": filtered_url, "check_status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            expected_ids = [
                row["path"] for row in initial["rows"]
                if (row["department"] or None) == label
            ]
            observed_ids = filtered["paths"]
            ordered = observed_ids == expected_ids
            membership_matches = (
                len(observed_ids) == len(expected_ids)
                and set(observed_ids) == set(expected_ids)
            )
            subset = set(observed_ids) <= set(initial_sequence)
            if filtered["issues"]:
                self.result.errors.extend(filtered["issues"])
            if not membership_matches:
                self.result.errors.append(f"okta_department_filter_membership_mismatch:{label}")
            if not subset:
                self.result.errors.append(f"okta_department_filter_unknown_job:{label}")
            department_checks[label] = {
                "value": value,
                "url": filtered_url,
                "check_status": "ok",
                "expected_rows": len(expected_ids),
                "observed_rows": len(observed_ids),
                "membership_matches": membership_matches,
                "ordered_membership_matches": ordered,
                "all_rows_in_unfiltered_listing": subset,
                "pagination_controls": filtered["pagination_controls"],
                "selected_filters": filtered["selected_filters"],
                **self._request_meta(response, seconds),
                "source_kind": "official_company_department_filter",
            }
        filter_seconds = max(0.0, time.perf_counter() - filter_started)
        self.result.timings["department_filter_checks_wall_seconds"] = filter_seconds
        self.result.metadata["department_filter_checks"] = department_checks
        ungrouped = [row for row in initial["rows"] if not row.get("department")]
        self.result.metadata["membership_shape"] = {
            "unfiltered_rows": initial["count"],
            "department_counts": {
                option["label"]: sum(1 for row in initial["rows"] if row.get("department") == option["label"])
                for option in department_options
            },
            "ungrouped_rows": len(ungrouped),
            "ungrouped_ids": [row["id"] for row in ungrouped],
            "department_rows_sum": sum(
                sum(1 for row in initial["rows"] if row.get("department") == option["label"])
                for option in department_options
            ),
            "filter_options": initial["filter_options"],
            "pagination_controls": initial["pagination_controls"],
            "note": "The unfiltered listing is the membership authority; department filters must partition the grouped rows exactly.",
        }
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)

        checkpoint = self.options.checkpoint
        self.options.checkpoint = None
        try:
            for row in initial["rows"]:
                self.add(self._make_job(row))
        finally:
            self.options.checkpoint = checkpoint
        if self.limit():
            self.result.listing_complete = False
        else:
            await self.hydrate(self.result.jobs, self._detail)

        final_started = time.perf_counter()
        final_response = final = None
        try:
            final_response, final_seconds = await self._text(LISTING_URL)
            final = parse_listing(final_response.text, str(final_response.url))
        except Exception as exc:
            final_seconds = max(0.0, time.perf_counter() - final_started)
            self.result.errors.append(
                f"okta_final_listing_recheck_failed:{type(exc).__name__}:{exc}")
            self.result.metadata.setdefault("failed_sources", []).append({
                "url": LISTING_URL,
                "reason": f"final listing recheck {type(exc).__name__}: {exc}",
            })
        final_seconds_wall = max(0.0, time.perf_counter() - final_started)
        if final is None:
            self.result.timings["final_listing_recheck_wall_seconds"] = final_seconds_wall
            self.result.listing_complete = False
            self.result.metadata["membership_reconciliation"] = {
                "initial_count": initial["count"],
                "final_count": None,
                "complete": False,
                "error": "final unfiltered listing recheck failed",
                "seconds": round(final_seconds_wall, 6),
            }
            self.result.metadata["final_listing"] = {
                "url": LISTING_URL,
                "error": self.result.errors[-1],
                "seconds": round(final_seconds_wall, 6),
            }
            final = {"rows": [], "paths": [], "job_ids": [], "issues": [],
                     "pagination_controls": [], "filter_options": initial["filter_options"],
                     "count": None}
        changes = {}
        final_by_id = {row["id"]: row for row in final["rows"]}
        for identity in sorted(set(initial_by_id) | set(final_by_id)):
            before, after = initial_by_id.get(identity), final_by_id.get(identity)
            if before is None or after is None:
                continue
            changed = [
                field for field in ("title", "location", "department", "path")
                if before.get(field) != after.get(field)
            ]
            if changed:
                changes[identity] = {
                    field: {"initial": before.get(field), "final": after.get(field)}
                    for field in changed
                }
        new_ids = [row["id"] for row in final["rows"] if row["id"] not in initial_by_id]
        removed_ids = [row["id"] for row in initial["rows"] if row["id"] not in final_by_id]
        same_order = final["paths"] == initial_sequence
        filters_same = final["filter_options"] == initial["filter_options"]
        if final_response is not None:
            self.result.metadata["final_listing"] = {
                **final,
                **self._request_meta(final_response, final_seconds),
                "request_url": LISTING_URL,
                "source_kind": "official_company_listing_unfiltered_final_recheck",
            }
        self.result.metadata["membership_reconciliation"] = {
            "initial_count": initial["count"],
            "final_count": final["count"],
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"],
            "new_ids": new_ids,
            "removed_ids": removed_ids,
            "same_order": same_order,
            "changed_fields_by_id": changes,
            "filter_options_unchanged": filters_same,
            "department_filter_checks_complete": all(
                check.get("check_status") == "ok"
                and check.get("membership_matches")
                and check.get("all_rows_in_unfiltered_listing")
                for check in department_checks.values()
            ),
            "complete": bool(
                initial["count"]
                and initial["count"] == final["count"]
                and not new_ids and not removed_ids and not changes and filters_same
                and all(
                    check.get("check_status") == "ok"
                    and check.get("membership_matches")
                    and check.get("all_rows_in_unfiltered_listing")
                    for check in department_checks.values()
                )
            ),
            "seconds": round(final_seconds_wall, 6),
        }
        self.result.timings["final_listing_recheck_wall_seconds"] = final_seconds_wall
        if final["issues"]:
            self.result.errors.extend(final["issues"])
        if final["pagination_controls"]:
            self.result.errors.append("okta_listing_pagination_present_final")
        if new_ids or removed_ids or changes:
            self.result.errors.append("okta_listing_membership_changed_during_collection")
        if not same_order:
            self.result.warnings.append("okta_listing_order_changed_during_collection")
        if not filters_same:
            self.result.errors.append("okta_listing_filters_changed_during_collection")
        self.result.listing_complete = bool(self.result.metadata["membership_reconciliation"]["complete"])
        if self.result.expected_total != len(self.result.jobs):
            self.result.errors.append("okta_listing_job_count_mismatch")

        audit_started = time.perf_counter()
        failed_jobs, field_issues = [], []
        for job in self.result.jobs:
            if not job.description_verified:
                failed_jobs.append({
                    "id": job.job_id,
                    "url": job.url,
                    "reason": "; ".join(job.warnings) or "description_not_verified",
                })
            detail = job.raw.get("detail") or {}
            listing = job.raw.get("listing") or {}
            if detail.get("title_matches_listing") is False:
                field_issues.append({"id": job.job_id, "issue": "detail_title_changed_from_listing"})
            if detail.get("location_matches_listing") is False:
                field_issues.append({"id": job.job_id, "issue": "detail_location_changed_from_listing"})
            if detail.get("article_chars", 0) < 100:
                field_issues.append({"id": job.job_id, "issue": "full_description_missing"})
            if not listing:
                field_issues.append({"id": job.job_id, "issue": "listing_row_missing"})
        self.result.metadata["local_audit"] = {
            "jobs_checked": len(self.result.jobs),
            "failed_jobs": failed_jobs,
            "field_issues": field_issues,
            "all_descriptions_verified": len(failed_jobs) == 0,
            "all_listing_rows_present": len(self.result.jobs) == initial["count"],
            "all_requisitions_public_or_null": all(
                job.requisition_id is None or isinstance(job.requisition_id, str)
                for job in self.result.jobs
            ),
            "dates_are_source_or_null": all(
                (job.date_posted is None or bool(ISO_DATE.fullmatch(job.date_posted)))
                for job in self.result.jobs
            ),
        }
        self.result.timings["local_audit_seconds"] = max(0.0, time.perf_counter() - audit_started)
        self.result.metadata["detail_source"] = (
            "Each row's exact company-owned /company/careers/<slug>/ page; full article.Job__content body. "
            "JSON-LD description is deliberately not used because Okta truncates it."
        )
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        self.result.metadata["request_scope"] = (
            "Only Okta's own careers entry, its own global unfiltered listing, its own department filters, "
            "and the exact company job detail URLs exposed by that listing. No ATS board or external board was queried."
        )
        self.result.metadata["official_entry_url"] = OFFICIAL_ENTRY
        self.result.metadata["listing_url"] = LISTING_URL
        return self.result
