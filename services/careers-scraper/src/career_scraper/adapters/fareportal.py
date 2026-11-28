"""Fareportal's own WordPress WP Job Openings archive (``/jobs/``) and detail pages.

The ledger entry ``/careers/`` is a WordPress 404 and the visible navigation
does not link a jobs page.  Fareportal's own Yoast sitemap index declares
``awsm_job_openings-sitemap.xml``; that sitemap lists the public ``/jobs/``
archive plus every published job detail.  The server-rendered archive is the
membership authority and each exact ``/jobs/<slug>/`` page carries the full
``.awsm-job-entry-content`` body plus a JSON-LD ``JobPosting`` record.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.fareportal.com/careers/"
OFFICIAL_HOME = "https://www.fareportal.com/"
PEOPLE_AND_CULTURE = "https://www.fareportal.com/people-and-culture/"
SITEMAP_INDEX_URL = "https://www.fareportal.com/sitemap_index.xml"
JOB_SITEMAP_PATH = "/awsm_job_openings-sitemap.xml"
JOB_SITEMAP_URL = "https://www.fareportal.com" + JOB_SITEMAP_PATH
LISTING_URL = "https://www.fareportal.com/jobs/"
SECOND_PAGE_URL = "https://www.fareportal.com/jobs/page/2/"
JOB_HOST = "www.fareportal.com"
JOB_PATH = re.compile(r"^/jobs/([a-z0-9][a-z0-9-]*)/$", re.I)
CARD_ID = re.compile(r"^awsm-grid-item-(\d+)$")
EMPLOYER = "Fareportal"

SPECIFICATION_KEYS = {
    "awsm-job-specification-job-category": "job-category",
    "awsm-job-specification-job-type": "job-type",
    "awsm-job-specification-job-location": "job-location",
}
HEADER_LABELS = {"title", "designation", "role", "location", "type", "salary range",
                 "shift timings", "job category", "job type"}
SECTION_HEADING = re.compile(
    r"(?i)^(job description|role overview|company overview|who we are|fareportal highlights|"
    r"about(?: us| the role| fareportal)?|key responsibilities|responsibilities|"
    r"roles? (?:&|and) responsibilities|job description and responsibilities|"
    r"role description(?: (?:&|and) responsibilities)?|duties|"
    r"requirements|qualifications|required qualifications|preferred qualifications|"
    r"requirements (?:&|and) qualifications|required qualifications/skills|"
    r"required skills(?: (?:&|and) qualification)?|skills(?: (?:&|and) qualifications?)?|"
    r"skill/qualification requirement|education|experience|shift timings|"
    r"benefits|perks|disclaimer)\b"
)
SECTION_HEADING_EXACT = {
    "job description", "role overview", "company overview", "who we are", "fareportal highlights",
    "key responsibilities", "responsibilities", "requirements", "qualifications",
    "required qualifications", "preferred qualifications", "about us", "about fareportal",
    "benefits", "perks", "disclaimer", "job overview", "role description",
    "roles & responsibilities", "roles and responsibilities", "education", "experience",
    "shift timings", "benefits & perks", "required qualifications/skills",
    "requirements & qualifications", "role description & responsibilities",
    "role description and responsibilities", "job description & responsibilities",
    "job description and responsibilities",
}
TYPOGRAPHIC = str.maketrans({
    "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u00d7": "x",
})


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    # BeautifulSoup separates adjacent inline nodes with a space; the rendered
    # body does not ("Qualifications</strong>:" renders "Qualifications:").
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return value.strip()


def comparison_text(html: str | None) -> str:
    """Normalise the typographic substitutions JSON-LD makes in its description copy."""
    return normalized_body_text(html).translate(TYPOGRAPHIC)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _iso(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _fareportal_job_url(value: str) -> str | None:
    parts = urlsplit(value or "")
    if parts.scheme != "https" or parts.netloc != JOB_HOST or JOB_PATH.fullmatch(parts.path) is None:
        return None
    return value


def _xml_sitemap_entries(xml_text: str) -> list[dict]:
    soup = BeautifulSoup(xml_text, "xml")
    entries = []
    for node in soup.find_all("sitemap"):
        loc = node.find("loc")
        lastmod = node.find("lastmod")
        if loc is not None:
            entries.append({"url": normalized_title(loc.get_text()), "lastmod": normalized_title(lastmod.get_text()) if lastmod else None})
    return entries


def parse_sitemap_index(xml_text: str, response_url: str = SITEMAP_INDEX_URL) -> dict:
    """Find the declared WP Job Openings sitemap in Fareportal's Yoast index."""
    if urlsplit(response_url).netloc != JOB_HOST:
        raise ValueError("Fareportal sitemap index came from an unexpected host")
    entries = _xml_sitemap_entries(xml_text)
    if not entries:
        raise ValueError("Fareportal sitemap index contains no sitemap entries")
    matches = [entry for entry in entries
               if urlsplit(entry["url"]).path.rstrip("/").endswith(JOB_SITEMAP_PATH)]
    if len(matches) != 1:
        raise ValueError("Fareportal sitemap index no longer declares exactly one job sitemap")
    job = matches[0]
    if urlsplit(job["url"]).netloc != JOB_HOST:
        raise ValueError("Fareportal job sitemap is not declared on the official company host")
    return {"sitemaps": entries, "job_sitemap_url": job["url"], "job_sitemap_lastmod": job["lastmod"]}


def parse_job_sitemap(xml_text: str, response_url: str = JOB_SITEMAP_URL) -> dict:
    """Validate the 18-loc job sitemap: the archive plus every published detail."""
    if urlsplit(response_url).netloc != JOB_HOST:
        raise ValueError("Fareportal job sitemap came from an unexpected host")
    soup = BeautifulSoup(xml_text, "xml")
    entries = []
    for node in soup.find_all("url"):
        loc = node.find("loc")
        lastmod = node.find("lastmod")
        if loc is None:
            raise ValueError("Fareportal job sitemap entry is missing its loc")
        url = normalized_title(loc.get_text())
        entries.append({"url": url, "lastmod": normalized_title(lastmod.get_text()) if lastmod else None})
    archives = [entry for entry in entries if urlsplit(entry["url"]).path.rstrip("/") == "/jobs"]
    details = [entry for entry in entries if _fareportal_job_url(entry["url"])]
    if len(archives) != 1:
        raise ValueError("Fareportal job sitemap no longer contains exactly one /jobs/ archive entry")
    if len(details) != len(entries) - 1:
        raise ValueError("Fareportal job sitemap contains a non-job URL")
    return {
        "archive_url": archives[0]["url"],
        "archive_lastmod": archives[0]["lastmod"],
        "entries": entries,
        "detail_urls": [entry["url"] for entry in details],
        "detail_lastmods": {entry["url"]: entry["lastmod"] for entry in details},
        "detail_count": len(details),
    }


def _specifications(scope) -> tuple[dict, dict]:
    specs, labels = {}, {}
    for item in scope.select(".awsm-job-specification-item"):
        classes = item.get("class") or []
        key = next((SPECIFICATION_KEYS[c] for c in classes if c in SPECIFICATION_KEYS), None)
        value_node = item.select_one(".awsm-job-specification-term")
        value = normalized_title(value_node.get_text(" ", strip=True)) if value_node else ""
        if key is None or not value:
            continue
        specs[key] = value
        label_node = item.select_one(".awsm-job-specification-label")
        if label_node is not None:
            labels[key] = normalized_title(label_node.get_text(" ", strip=True)).rstrip(":").strip()
    return specs, labels


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    """Parse the server-rendered WP Job Openings archive cards."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != JOB_HOST:
        raise ValueError("Fareportal archive response came from an unexpected host")
    if parts.path.rstrip("/") not in {"/jobs", "/jobs/page/2"}:
        raise ValueError("Fareportal archive response is not the inspected /jobs/ route")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Job Openings Archive" not in page_title:
        raise ValueError("Fareportal /jobs/ page title changed")

    cards, issues = [], []
    seen_ids, seen_urls = set(), set()
    for position, wrapper in enumerate(soup.select('div.awsm-grid-item[id^="awsm-grid-item-"]'), 1):
        match = CARD_ID.fullmatch(wrapper.get("id") or "")
        if match is None:
            issues.append("unparseable_card_container_id")
            continue
        identity = match.group(1)
        anchor = wrapper.select_one("a.awsm-job-item")
        if anchor is None or not anchor.get("href"):
            issues.append(f"missing_card_link:{identity}")
            continue
        url = urljoin(response_url, anchor["href"])
        if _fareportal_job_url(url) is None:
            issues.append(f"unexpected_card_link:{identity}")
            continue
        title_node = wrapper.select_one(".awsm-job-post-title")
        title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
        if not title:
            issues.append(f"missing_card_title:{identity}")
            continue
        specs, labels = _specifications(wrapper)
        if identity in seen_ids:
            issues.append(f"duplicate_card_id:{identity}")
            continue
        if url in seen_urls:
            issues.append(f"duplicate_card_url:{identity}")
            continue
        seen_ids.add(identity)
        seen_urls.add(url)
        cards.append({"position": position, "id": identity, "url": url, "title": title,
                      "specs": specs, "spec_labels": labels})
    if not cards:
        issues.append("fareportal_archive_has_no_job_cards")

    pagination_controls = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select(
        ".awsm-job-pagination a, .awsm-job-pagination button, nav.pagination a, a.page-numbers, a.next, a.prev")]
    declared_pagination = [{"rel": node.get("rel"), "href": urljoin(response_url, node["href"])}
                           for node in soup.select('link[rel="next"][href], link[rel="prev"][href]')]
    filters = {}
    for select in soup.select("select[name^='awsm_job_spec']"):
        filters[select.get("name")] = [option.get("value") for option in select.select("option")]
    filter_form = soup.select_one("form[action*='admin-ajax.php']")
    return {
        "url": response_url,
        "page_title": page_title,
        "cards": cards,
        "job_ids": [card["id"] for card in cards],
        "count": len(cards),
        "pagination_controls": pagination_controls,
        "declared_pagination_links": declared_pagination,
        "filters": filters,
        "filter_form_action": filter_form.get("action") if filter_form is not None else None,
        "issues": issues,
    }


def parse_header_fields(description_text: str) -> dict:
    """Read the leading ``Title:``/``Location:``/``Type:`` block that lives inside the body."""
    lines = [line.strip() for line in (description_text or "").splitlines() if line.strip()][:14]
    fields, index = {}, 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z][A-Za-z /&-]{1,30})\s*:\s*(.*)$", lines[index])
        if match is None or match.group(1).strip().lower() not in HEADER_LABELS:
            break
        label, value = match.group(1).strip().lower(), match.group(2).strip()
        if not value and index + 1 < len(lines):
            following = lines[index + 1]
            if re.match(r"^[A-Za-z][A-Za-z /&-]{1,30}\s*:", following) is None:
                value = following
                index += 1
        fields[label] = value
        index += 1
    return fields


def description_sections(description_text: str) -> list[dict]:
    """Split the employer body on its own short heading lines (metadata only)."""
    lines = [line.strip() for line in (description_text or "").splitlines() if line.strip()]
    sections, current = [], None
    for line in lines:
        candidate = line.rstrip(":").strip()
        is_heading = len(candidate) <= 90 and not re.search(r"\.\s", candidate) and (
            (line.endswith(":") and SECTION_HEADING.match(candidate) is not None)
            or candidate.lower() in SECTION_HEADING_EXACT)
        if is_heading:
            if current is not None:
                sections.append(current)
            current = {"heading": candidate, "text": ""}
        elif current is not None:
            current["text"] = (current["text"] + "\n" + line).strip()
    if current is not None:
        sections.append(current)
    return sections


def parse_application_form(soup: BeautifulSoup) -> dict:
    form = soup.select_one("form#awsm-application-form, form.awsm-application-form")
    if form is None:
        return {"present": False, "external_ats_url": None}
    container = form.find_parent("div", class_="awsm-job-form")
    heading = None
    if container is not None:
        node = container.select_one("h1, h2, h3, h4")
        heading = normalized_title(node.get_text(" ", strip=True)) if node is not None else None
    fields = []
    for element in form.select("input, textarea, select"):
        field_type = (element.get("type") or element.name).lower()
        if field_type in {"hidden", "submit", "button", "reset"}:
            continue
        identifier = element.get("id")
        label = form.select_one(f'label[for="{identifier}"]') if identifier else None
        record = {
            "name": element.get("name"),
            "type": field_type,
            "required": element.has_attr("required"),
            "label": normalized_title(label.get_text(" ", strip=True)).rstrip("*").strip() if label is not None else None,
        }
        if element.get("accept"):
            record["accept"] = element.get("accept")
        fields.append(record)
    hidden = {element.get("name"): element.get("value")
              for element in form.select('input[type="hidden"]') if element.get("name")}
    return {
        "present": True,
        "heading": heading,
        "method": (form.get("method") or "").lower() or None,
        "action": form.get("action"),
        "enctype": form.get("enctype"),
        "fields": fields,
        "job_id_field": hidden.get("awsm_job_id"),
        "submission_action": hidden.get("action"),
        "external_ats_url": None,
        "note": "In-page WP Job Openings application form; no separate apply/ATS URL is published.",
    }


def _job_posting(soup: BeautifulSoup) -> dict | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        candidates = value if isinstance(value, list) else [value]
        if isinstance(value, dict) and isinstance(value.get("@graph"), list):
            candidates = value["@graph"]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def parse_detail(html: str, response_url: str, expected_card: dict | None = None) -> dict:
    """Parse one exact ``/jobs/<slug>/`` detail page and its JSON-LD record."""
    if _fareportal_job_url(response_url) is None:
        raise ValueError("Fareportal detail response is not an exact official /jobs/ page")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = soup.select_one("h1")
    if h1 is None:
        raise ValueError("Fareportal detail page has no job heading")
    title = normalized_title(h1.get_text(" ", strip=True))
    entry = soup.select_one(".awsm-job-entry-content")
    if entry is None:
        raise ValueError("Fareportal detail page no longer exposes .awsm-job-entry-content")
    description_html = "".join(str(child) for child in entry.contents).strip()
    body = normalized_body_text(description_html)
    if len(body) < 200:
        raise ValueError("Fareportal detail description body is empty or truncated")
    description_text = text_html(description_html)
    specs, spec_labels = _specifications(soup.select_one(".awsm-job-specifications-container") or soup)
    header_fields = parse_header_fields(description_text)
    sections = description_sections(description_text)
    application_form = parse_application_form(soup)
    issues, warnings = [], []

    if expected_card is not None:
        if normalized_title(expected_card.get("title")) != title:
            issues.append(f"card_detail_title_mismatch:{expected_card.get('id')}")
        if application_form.get("job_id_field") and str(application_form["job_id_field"]) != str(expected_card.get("id")):
            issues.append(f"card_detail_job_id_mismatch:{expected_card.get('id')}")
        card_specs = {key: value for key, value in (expected_card.get("specs") or {}).items()
                      if key in SPECIFICATION_KEYS.values()}
        if card_specs and specs and card_specs != specs:
            issues.append(f"card_detail_specification_mismatch:{expected_card.get('id')}")

    jsonld = _job_posting(soup)
    date_posted = date_posted_raw = valid_through = employment_code = employer = None
    jsonld_title = None
    entry_matches_jsonld = None
    if jsonld is None:
        warnings.append("missing_jsonld_jobposting")
    else:
        jsonld_title = normalized_title(unescape(str(jsonld.get("title") or "")))
        if jsonld_title and jsonld_title != title:
            warnings.append("jsonld_title_differs_from_visible_h1")
        date_posted_raw = jsonld.get("datePosted")
        date_posted = _iso(date_posted_raw)
        if date_posted_raw is not None and date_posted is None:
            issues.append("jsonld_date_posted_not_iso")
        valid_through = _iso(jsonld.get("validThrough"))
        employment_code = jsonld.get("employmentType")
        organization = jsonld.get("hiringOrganization")
        if isinstance(organization, dict):
            employer = normalized_title(organization.get("name"))
        if employer != EMPLOYER:
            issues.append("jsonld_hiring_organization_changed")
        description_value = jsonld.get("description")
        entry_matches_jsonld = (isinstance(description_value, str)
                                and comparison_text(description_value) == comparison_text(description_html))
        if not entry_matches_jsonld:
            issues.append("jsonld_description_differs_from_visible_body")

    responsibilities = [section for section in sections
                        if re.search(r"responsib|duties", section["heading"], re.I)]
    qualifications = [section for section in sections
                      if re.search(r"qualif|requirement|skills", section["heading"], re.I)]
    return {
        "url": response_url,
        "page_title": page_title,
        "title": title,
        "specs": specs,
        "spec_labels": spec_labels,
        "header_fields": header_fields,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(body),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(body),
        "sections": sections,
        "section_headings": [section["heading"] for section in sections],
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "jsonld": None if jsonld is None else {key: value for key, value in jsonld.items() if key != "description"},
        "jsonld_description_sha256": sha256(str(jsonld.get("description") or "").encode()).hexdigest() if jsonld else None,
        "jsonld_title": jsonld_title,
        "entry_matches_jsonld_description": entry_matches_jsonld,
        "date_posted": date_posted,
        "date_posted_raw": date_posted_raw,
        "valid_through": valid_through,
        "employment_type_code": employment_code,
        "employer": employer,
        "application_form": application_form,
        "issues": issues,
        "warnings": warnings,
    }


class Fareportal(Adapter):
    """Read only Fareportal's own sitemap-declared /jobs/ archive and detail pages."""

    VERIFICATION_REVISION = "fareportal.official-wp-job-openings-archive.1"

    async def _timed_get(self, url: str):
        started = time.perf_counter()
        response = await self.http.get(url)
        return response, max(0.0, time.perf_counter() - started)

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        entry = {"requested_url": OFFICIAL_ENTRY, "final_url": OFFICIAL_ENTRY, "status": None}
        try:
            response = await self.http.get(OFFICIAL_ENTRY)
            entry.update(status=response.status_code, final_url=str(response.url),
                         response_bytes=len(response.content),
                         response_sha256=sha256(response.content).hexdigest())
        except FetchError as exc:
            match = re.search(r"HTTP (\d{3})", str(exc))
            entry.update(status=int(match.group(1)) if match else None, error=str(exc))
        entry["seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return entry

    async def _sitemaps(self) -> dict:
        started = time.perf_counter()
        index_response, index_seconds = await self._timed_get(SITEMAP_INDEX_URL)
        index = parse_sitemap_index(index_response.text, str(index_response.url))
        job_response, job_seconds = await self._timed_get(index["job_sitemap_url"])
        job = parse_job_sitemap(job_response.text, str(job_response.url))
        return {
            "index": index,
            "job": job,
            "index_metadata": {"requested_url": SITEMAP_INDEX_URL, "final_url": str(index_response.url),
                               "status": index_response.status_code, "seconds": round(index_seconds, 6),
                               "response_bytes": len(index_response.content),
                               "response_sha256": sha256(index_response.content).hexdigest()},
            "job_metadata": {"requested_url": index["job_sitemap_url"], "final_url": str(job_response.url),
                             "status": job_response.status_code, "seconds": round(job_seconds, 6),
                             "response_bytes": len(job_response.content),
                             "response_sha256": sha256(job_response.content).hexdigest()},
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _listing(self, url: str = LISTING_URL) -> dict:
        if not self.page_guard():
            raise ValueError("Fareportal listing page budget exhausted")
        response, seconds = await self._timed_get(url)
        parsed = parse_listing(response.text, str(response.url))
        parsed.update(status=response.status_code, final_url=str(response.url),
                      seconds=round(seconds, 6), response_bytes=len(response.content),
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    async def _page_two_probe(self) -> dict:
        try:
            probe = await self._listing(SECOND_PAGE_URL)
        except Exception as exc:  # a probe failure must not hide the real archive
            return {"url": SECOND_PAGE_URL, "status": None, "error": f"{type(exc).__name__}: {exc}",
                    "count": None, "job_ids": [], "repeats_page_one": None}
        return {"url": probe["url"], "status": probe["status"], "count": probe["count"],
                "job_ids": probe["job_ids"], "pagination_controls": probe["pagination_controls"],
                "declared_pagination_links": probe["declared_pagination_links"],
                "issues": probe["issues"], "seconds": probe["seconds"]}

    async def detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        parsed = parse_detail(response.text, str(response.url), job.raw["listing"])
        seconds = max(0.0, time.perf_counter() - started)
        parsed["response"] = {
            "status": response.status_code,
            "final_url": str(response.url),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "request_seconds": round(seconds, 6),
        }
        job.raw["detail"] = parsed
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "fareportal.wp-job-openings.awsm-job-entry-content"
        job.description_source_url = str(response.url)
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed["valid_through"]
        job.date_updated = None
        job.employer = parsed["employer"] or EMPLOYER
        listing = job.raw["listing"]
        job.department = listing["specs"].get("job-category") or parsed["specs"].get("job-category")
        job.employment_type = listing["specs"].get("job-type") or None
        job.workplace_type = parsed["header_fields"].get("type") or None
        header_location = parsed["header_fields"].get("location")
        card_location = listing["specs"].get("job-location")
        job.locations = [card_location or header_location] if (card_location or header_location) else []
        job.salary = parsed["header_fields"].get("salary range") or None
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.raw["detail"]["location_sources"] = {"listing_card": card_location, "body_header": header_location}
        job.raw["detail"]["header_title"] = (parsed["header_fields"].get("title")
                                            or parsed["header_fields"].get("designation")
                                            or parsed["header_fields"].get("role"))
        sitemap_lastmod = job.raw.get("sitemap", {}).get("lastmod")
        job.raw["detail"]["sitemap_lastmod"] = sitemap_lastmod
        job.raw["detail"]["sitemap_lastmod_matches_date_posted"] = bool(
            sitemap_lastmod and parsed["date_posted"] and sitemap_lastmod == parsed["date_posted"])
        for issue in parsed["issues"]:
            self.result.errors.append(f"Fareportal detail {job.job_id}: {issue}")
        for warning in parsed["warnings"]:
            job.warnings.append(warning)
        if parsed["date_posted"] is None:
            job.warnings.append("date_posted_unknown")
        if parsed["application_form"].get("present"):
            job.raw["detail"]["application_route"] = {
                "type": "in_page_wp_job_openings_form",
                "apply_url": None,
                "detail_page_url": job.url,
                "submission_action": parsed["application_form"].get("submission_action"),
                "external_ats_url": None,
            }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_home": OFFICIAL_HOME,
            "official_people_and_culture": PEOPLE_AND_CULTURE,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "Fareportal's own server-rendered /jobs/ WordPress WP Job Openings archive, declared "
                "in its Yoast sitemap index (awsm_job_openings-sitemap.xml). The ledger /careers/ "
                "route is a WordPress 404 and no jobs link appears in the visible navigation."
            ),
            "discovery_path": {
                "ledger_entry": OFFICIAL_ENTRY,
                "ledger_entry_expected_status": 404,
                "sitemap_index": SITEMAP_INDEX_URL,
                "declared_job_sitemap": JOB_SITEMAP_URL,
                "archive": LISTING_URL,
                "jobs_link_in_visible_navigation": False,
            },
            "detail_method": "Full server-rendered .awsm-job-entry-content body from each exact /jobs/<slug>/ page.",
            "request_scope": (
                "Only www.fareportal.com: ledger entry, sitemap index, its declared job sitemap, the "
                "/jobs/ archive, one page-2 probe and the exact detail links listed there. No ATS or "
                "third-party board is queried."
            ),
        })
        try:
            entry = await self._official_entry()
            sitemaps = await self._sitemaps()
            listing = await self._listing()
            page_two = await self._page_two_probe()
        except Exception as exc:
            self.result.errors.append(f"Fareportal source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.timings["sitemap_seconds"] = sitemaps["seconds"]
        self.result.timings["listing_seconds"] = listing["seconds"]
        self.result.timings["page_two_probe_seconds"] = page_two.get("seconds", 0.0)
        self.result.timings["source_collection_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6)
        self.result.metadata["official_entry"] = entry
        self.result.metadata["sitemaps"] = {
            "index_metadata": sitemaps["index_metadata"],
            "job_metadata": sitemaps["job_metadata"],
            "sitemap_count": len(sitemaps["index"]["sitemaps"]),
            "declared_job_sitemap": sitemaps["index"]["job_sitemap_url"],
            "job_sitemap_detail_count": sitemaps["job"]["detail_count"],
            "job_sitemap_lastmod": sitemaps["job"]["archive_lastmod"],
        }
        self.result.metadata["initial_listing"] = {key: value for key, value in listing.items() if key != "cards"}
        self.result.metadata["page_two_probe"] = page_two
        if entry.get("status") != 404:
            self.result.warnings.append("official_entry_status_changed")
        for issue in listing["issues"]:
            self.result.errors.append(issue)

        cards = listing["cards"]
        if not cards:
            self.result.errors.append("fareportal_archive_membership_empty")
            self.result.listing_complete = False
            return self.result
        self.result.expected_total = listing["count"]
        card_urls = [card["url"] for card in cards]
        sitemap_urls = sitemaps["job"]["detail_urls"]
        if set(card_urls) != set(sitemap_urls):
            self.result.errors.append("fareportal_sitemap_and_archive_membership_mismatch")
        page_two_ids = page_two.get("job_ids") or []
        if page_two_ids and page_two_ids != listing["job_ids"]:
            self.result.warnings.append("page_two_probe_differs_from_page_one")

        lastmods = sitemaps["job"]["detail_lastmods"]
        jobs = []
        for card in cards:
            job = self.job(
                {"listing": card, "sitemap": {"lastmod": lastmods.get(card["url"]),
                                              "job_sitemap": sitemaps["job"]["archive_url"]}},
                title=card["title"],
                url=card["url"],
                job_id=card["id"],
                description="",
                locations=[card["specs"]["job-location"]] if card["specs"].get("job-location") else [],
                department=card["specs"].get("job-category"),
                employment_type=card["specs"].get("job-type"),
                employer=EMPLOYER,
                language="en",
            )
            jobs.append(job)
            self.add(job)

        detail_started = time.perf_counter()
        await self.hydrate(self.result.jobs, self.detail)
        self.result.timings["detail_wall_seconds"] = round(max(0.0, time.perf_counter() - detail_started), 6)

        final_started = time.perf_counter()
        try:
            final_sitemaps = await self._sitemaps()
            final_listing = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Fareportal final source recheck failed: {type(exc).__name__}: {exc}")
            final_sitemaps = final_listing = None
        self.result.timings["final_recheck_seconds"] = round(
            max(0.0, time.perf_counter() - final_started), 6)

        initial_by_id = {card["id"]: card for card in cards}
        if final_listing is not None and final_sitemaps is not None:
            final_by_id = {card["id"]: card for card in final_listing["cards"]}
            changed = {}
            for identity in listing["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key))
            same_ids = listing["job_ids"] == final_listing["job_ids"]
            same_sitemap = set(sitemaps["job"]["detail_urls"]) == set(final_sitemaps["job"]["detail_urls"])
            complete = (same_ids and same_sitemap and not changed
                        and final_listing["count"] == listing["count"] == len(self.result.jobs))
            self.result.metadata["final_listing"] = {key: value for key, value in final_listing.items() if key != "cards"}
            self.result.metadata["final_sitemaps"] = {
                "job_metadata": final_sitemaps["job_metadata"],
                "job_sitemap_detail_count": final_sitemaps["job"]["detail_count"],
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": listing["job_ids"],
                "final_ids": final_listing["job_ids"],
                "sitemap_detail_urls": sitemaps["job"]["detail_urls"],
                "final_sitemap_detail_urls": final_sitemaps["job"]["detail_urls"],
                "page_two_ids": page_two_ids,
                "initial_count": listing["count"],
                "final_count": final_listing["count"],
                "sitemap_detail_count": sitemaps["job"]["detail_count"],
                "sitemap_and_archive_urls_match": set(card_urls) == set(sitemap_urls),
                "same_ids_and_order": same_ids,
                "same_sitemap_urls": same_sitemap,
                "all_card_fields_match": not changed,
                "changed_fields_by_job_id": changed,
                "new_job_ids": [identity for identity in final_listing["job_ids"] if identity not in initial_by_id],
                "removed_job_ids": [identity for identity in listing["job_ids"] if identity not in final_by_id],
                "page_two_repeats_page_one": bool(page_two_ids) and page_two_ids == listing["job_ids"],
                "complete": complete,
            }
            if not same_ids:
                self.result.errors.append("fareportal_listing_membership_changed_during_collection")
            if not same_sitemap:
                self.result.errors.append("fareportal_sitemap_membership_changed_during_collection")
            if changed:
                self.result.errors.append("fareportal_listing_card_fields_changed_during_collection")
            self.result.listing_complete = bool(complete)
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": listing["job_ids"],
                "sitemap_detail_urls": sitemaps["job"]["detail_urls"],
                "page_two_ids": page_two_ids,
                "initial_count": listing["count"],
                "sitemap_detail_count": sitemaps["job"]["detail_count"],
                "sitemap_and_archive_urls_match": set(card_urls) == set(sitemap_urls),
                "complete": False,
            }
            self.result.listing_complete = False

        verified = [job for job in self.result.jobs if job.description_verified]
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["descriptions_verified"] = len(verified)
        self.result.metadata["descriptions_unavailable"] = [job.job_id for job in self.result.jobs
                                                            if job.description_unavailable]
        self.result.metadata["detail_hydration_mode"] = (
            "One exact /jobs/<slug>/ GET per archive card; the full .awsm-job-entry-content body is stored.")
        posted_dates = sorted(job.date_posted for job in self.result.jobs if job.date_posted)
        self.result.metadata["dates"] = {
            "source": "JSON-LD JobPosting.datePosted on each detail page",
            "date_posted_count": len(posted_dates),
            "date_posted_range": [posted_dates[0], posted_dates[-1]] if posted_dates else None,
            "valid_through_values": sorted({job.valid_through for job in self.result.jobs
                                            if job.valid_through is not None}),
            "date_updated_available": False,
            "visible_dates_on_detail_pages": False,
        }
        self.result.metadata["apply_route"] = (
            "No separate apply/ATS URL is published. Each detail page embeds an in-page WP Job "
            "Openings application form (submission_action=awsm_applicant_form_submission) and no "
            "external ATS link; apply_url remains null.")
        self.result.metadata["source_collection_seconds"] = self.result.timings["source_collection_seconds"]
        return self.result
