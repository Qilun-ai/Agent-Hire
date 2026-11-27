"""Jupiter's official careers page, its exact Keka iframe, and that portal's active-job feed."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://jupiter.money/careers/"
KEKA_CAREERS = "https://jupiter.keka.com/careers"
KEKA_ORIGIN = "https://jupiter.keka.com"
EXPECTED_PORTAL_IDENTIFIER = "b5279857-cf81-4dde-a215-fc48957ee2b5"
EXPECTED_PORTAL_NAME = "Jupiter Money"
EXPECTED_IFRAME_ID = "mnhembedded"
PORTAL_FETCH = re.compile(
    r"fetch\s*\(\s*(['\"])(/ats/documents/([0-9a-f-]+)/careerportal/[0-9a-f]+\.html)\1\s*\)"
)
KH_CONFIG = re.compile(r"window\.khConfig\s*=\s*\{([^}]+)\}")
IDENTIFIER = re.compile(r"identifier:\s*'([^']+)'")
DOMAIN = re.compile(r"domain:\s*'([^']+)'")
TARGET_CONTAINER = re.compile(r"targetContainer:\s*'([^']+)'")
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
JOB_DETAIL_PATH = re.compile(r"^/careers/jobdetails/(\d+)$")
APPLY_PATH = re.compile(r"^/careers/applyjob/(\d+)$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tr", "td", "th", "ul",
})
TOP_LEVEL_HEADING_PATTERNS = (
    r"about\b",
    r"who we are\b",
    r"who we're looking for\b",
    r"our journey\b",
    r"what we do\b",
    r"the role\b",
    r"the bet\b",
    r"the team\b",
    r"the basics\b",
    r"responsibilities?\b",
    r"roles?\s*(?:&|and)\s*responsibilities\b",
    r"requirements?\b",
    r"nice to have\b",
    r"why\b",
    r"what is needed\b",
    r"what we're looking for\b",
    r"what you'll own\b",
    r"what you won't do\b",
    r"what success\b",
    r"what makes this different\b",
    r"how to apply\b",
    r"brownie points\b",
    r"want to know more\b",
)
RESPONSIBILITY_HEADING = re.compile(
    r"(?:responsibilit|roles?\s*(?:&|and)\s*responsibilities|what you will do|"
    r"what you'll do|what you'll own|key responsibilities|your role)", re.I
)
QUALIFICATION_HEADING = re.compile(
    r"(?:qualif|requirement|what is needed|what you will need|what you'll need|"
    r"who you are|who we're looking for|what we're looking for|skills|experience)", re.I
)
JOB_TYPE_LABELS = {1: "Part-Time", 2: "Full-Time"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def clean_heading(value: str | None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def visible_text(html: str | None) -> str:
    """Browser-like text: block boundaries become newlines, adjacent inline nodes join."""
    soup = BeautifulSoup(f'<div data-jupiter-text="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-jupiter-text]")
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name in {"script", "style", "noscript"}:
            return
        if name == "br":
            parts.append("\n")
            return
        block = name in BLOCK_TAGS
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    if root is not None:
        for child in root.children:
            walk(child)
    text = "".join(parts).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def compact_text(value: str | None) -> str:
    return re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _heading_text(block: Tag) -> str | None:
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        text = clean_heading(block.get_text(" ", strip=True))
        return text or None
    if block.name not in {"div", "p"}:
        return None
    if block.find(["ul", "ol", "li"]):
        return None
    strong = block.find("strong")
    if strong is None:
        return None
    text = clean_heading(block.get_text(" ", strip=True))
    strong_text = clean_heading(strong.get_text(" ", strip=True))
    if not text or not strong_text or len(text) > 120:
        return None
    remainder = text[len(strong_text):]
    if text != strong_text and not re.fullmatch(r"[.:;!?()\s-]*", remainder):
        return None
    return text


def description_sections(html: str | None) -> list[dict]:
    """Keep each employer-authored heading and the exact blocks below it."""
    soup = BeautifulSoup(f'<div data-jupiter-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-jupiter-body]")
    sections: list[dict] = []
    current = {"heading": None, "html": "", "text": ""}
    for block in (root.children if root is not None else []):
        if not isinstance(block, Tag):
            continue
        heading = _heading_text(block)
        if heading:
            if current["heading"] is not None or current["html"]:
                current["text"] = visible_text(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
        else:
            current["html"] += str(block)
    if current["heading"] is not None or current["html"]:
        current["text"] = visible_text(current["html"])
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    return sections


def grouped_sections(sections: list[dict]) -> list[dict]:
    """Group sub-headings under the nearest employer-authored top-level heading."""
    groups: list[dict] = []
    current: dict | None = None
    for section in sections:
        heading = section["heading"]
        if heading is None:
            if current is None:
                current = {"heading": None, "sections": []}
                groups.append(current)
            current["sections"].append(section)
            continue
        if any(re.match(pattern, heading, re.I) for pattern in TOP_LEVEL_HEADING_PATTERNS):
            current = {"heading": heading, "sections": [section]}
            groups.append(current)
        elif current is not None:
            current["sections"].append(section)
        else:
            current = {"heading": heading, "sections": [section]}
            groups.append(current)
    for group in groups:
        group["text"] = "\n".join(
            (section["heading"] + "\n" if section["heading"] else "") + section["text"]
            for section in group["sections"]
        ).strip()
    return groups


def _matching_groups(groups: list[dict], pattern: re.Pattern) -> list[dict]:
    return [group for group in groups if group["heading"] and pattern.search(group["heading"])]


def employment_type_label(value) -> str | None:
    return JOB_TYPE_LABELS.get(value)


def detail_url_for(job_id) -> str:
    identity = str(job_id)
    if not identity.isdigit():
        raise ValueError(f"Jupiter Keka job ID is not numeric: {job_id!r}")
    return f"{KEKA_ORIGIN}/careers/jobdetails/{identity}"


def apply_url_for(job_id) -> str:
    identity = str(job_id)
    if not identity.isdigit():
        raise ValueError(f"Jupiter Keka job ID is not numeric: {job_id!r}")
    return f"{KEKA_ORIGIN}/careers/applyjob/{identity}"


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "jupiter.money"
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Jupiter official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "careers" not in title.lower():
        raise ValueError("Jupiter official careers entry title is no longer the Careers page")
    iframes = soup.select(f"iframe#{EXPECTED_IFRAME_ID}")
    if len(iframes) != 1:
        raise ValueError(
            f"Jupiter official careers entry no longer exposes exactly one #{EXPECTED_IFRAME_ID} job iframe"
        )
    iframe = iframes[0]
    iframe_url = urljoin(response_url, iframe.get("src", ""))
    if iframe_url.rstrip("/") != KEKA_CAREERS:
        raise ValueError("Jupiter official careers job iframe no longer points at the inspected Keka portal")
    view_all = [
        {"text": anchor.get_text(" ", strip=True), "url": urljoin(response_url, anchor.get("href", ""))}
        for anchor in soup.select("a[href]")
        if normalized_title(anchor.get_text(" ", strip=True)).lower() == "view all openings"
    ]
    if not any(item["url"].rstrip("/") == KEKA_CAREERS for item in view_all):
        raise ValueError("Jupiter official careers page no longer links 'View all openings' to the Keka portal")
    return {
        "url": response_url,
        "title": title,
        "iframe_id": iframe.get("id"),
        "iframe_title": iframe.get("title"),
        "iframe_url": iframe_url,
        "view_all_openings": view_all,
        "headings": [
            normalized_title(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalized_title(node.get_text(" ", strip=True))
        ],
    }


def parse_keka_landing(html: str, response_url: str = KEKA_CAREERS) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "jupiter.keka.com"
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Jupiter Keka careers landing redirected away from the inspected portal")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "jupiter" not in title.lower():
        raise ValueError("Jupiter Keka careers landing no longer identifies Jupiter")
    matches = PORTAL_FETCH.findall(html)
    paths = sorted({match[1] for match in matches})
    if len(paths) != 1:
        raise ValueError(
            f"Jupiter Keka careers landing no longer exposes exactly one careerportal document fetch ({len(paths)})"
        )
    path = paths[0]
    path_match = re.fullmatch(r"/ats/documents/([0-9a-f-]+)/careerportal/[0-9a-f]+\.html", path)
    if path_match is None or not UUID.fullmatch(path_match.group(1)):
        raise ValueError("Jupiter Keka careerportal document path no longer identifies a portal")
    document_url = urljoin(response_url, path)
    document_parts = urlsplit(document_url)
    if (document_parts.scheme != "https" or document_parts.netloc != "jupiter.keka.com"
            or not document_parts.path.startswith("/ats/documents/")):
        raise ValueError("Jupiter Keka careerportal document URL left the observed portal host")
    return {
        "url": response_url,
        "title": title,
        "portal_document_url": document_url,
        "portal_document_path": path,
        "portal_document_identifier": path_match.group(1),
        "is_careers_page": "window.isCareersPage = true" in html,
    }


def parse_careerportal_document(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "jupiter.keka.com" or not parts.path.startswith("/ats/documents/"):
        raise ValueError("Jupiter careerportal document URL is not the observed Keka document route")
    config = KH_CONFIG.search(html)
    if not config:
        raise ValueError("Jupiter careerportal document no longer exposes window.khConfig")
    body = config.group(1)
    identifier = IDENTIFIER.search(body)
    domain = DOMAIN.search(body)
    target = TARGET_CONTAINER.search(body)
    if not identifier or not domain:
        raise ValueError("Jupiter khConfig is missing identifier/domain")
    identifier = identifier.group(1).strip()
    domain = domain.group(1).strip()
    if identifier != EXPECTED_PORTAL_IDENTIFIER:
        raise ValueError(f"Jupiter Keka portal identifier changed from the inspected tenant: {identifier}")
    if domain != "https://jupiter.keka.com/careers/":
        raise ValueError(f"Jupiter khConfig domain changed from the inspected Keka careers domain: {domain}")
    script = f"{KEKA_ORIGIN}/careers/api/embedjobs/js/{identifier}"
    if script not in html:
        raise ValueError("Jupiter careerportal document no longer loads the observed embedjobs script")
    return {
        "url": response_url,
        "identifier": identifier,
        "domain": domain,
        "target_container": target.group(1).strip() if target else None,
        "embedjobs_script_url": script,
    }


def parse_careerportal_info(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Jupiter careerportal info response is not an object")
    if value.get("name") != EXPECTED_PORTAL_NAME or value.get("shortName") != EXPECTED_PORTAL_NAME:
        raise ValueError("Jupiter careerportal identity changed from Jupiter Money")
    if value.get("careersPortalDomain") != "jupiter.keka.com":
        raise ValueError("Jupiter careerportal domain changed from jupiter.keka.com")
    settings = value.get("jobListingSetting")
    if not isinstance(settings, dict):
        raise ValueError("Jupiter careerportal jobListingSetting is missing")
    if settings.get("filters") != ["department", "location"]:
        raise ValueError("Jupiter careerportal filters changed from department/location")
    if settings.get("jobFields") != ["location", "experience", "jobType"]:
        raise ValueError("Jupiter careerportal visible job fields changed")
    if settings.get("jobListingFormat") != 1:
        raise ValueError("Jupiter careerportal listing format changed")
    return value


def parse_group_link_status(value) -> dict:
    if not isinstance(value, dict) or value.get("isGroupLinked") is not False:
        raise ValueError(
            "Jupiter Keka portal is group-linked or its status changed; the listing may include other companies"
        )
    return value


def parse_departments(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("Jupiter Keka departments response is empty or not a list")
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("Jupiter Keka departments response contains a non-object row")
        identifier = str(row.get("identifier") or "")
        if not UUID.fullmatch(identifier) or not normalized_title(row.get("name")):
            raise ValueError(f"Jupiter Keka department row is missing a valid identifier/name: {row!r}")
        if identifier in seen:
            raise ValueError(f"Jupiter Keka departments response repeats identifier {identifier}")
        seen.add(identifier)
    return value


def _parse_published_on(value, identity) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Jupiter Keka job {identity} is missing publishedOn")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Jupiter Keka job {identity} has an unparseable publishedOn: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Jupiter Keka job {identity} publishedOn has no timezone: {value!r}")
    return parsed


def parse_active_jobs(value, departments: list[dict] | None = None) -> dict:
    if not isinstance(value, list):
        raise ValueError("Jupiter Keka active-job response is not a list")
    if not value:
        raise ValueError("Jupiter Keka active-job response is empty; independent UI review is required")
    department_ids = {str(row["identifier"]) for row in departments or []}
    rows, seen, issues = [], set(), []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("Jupiter Keka active-job response contains a non-object row")
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            raise ValueError(f"Jupiter Keka active-job row has no positive integer id: {identity!r}")
        identity = str(identity)
        if identity in seen:
            raise ValueError(f"Jupiter Keka active-job response repeats job id {identity}")
        seen.add(identity)
        title = normalized_title(row.get("title"))
        if not title:
            raise ValueError(f"Jupiter Keka job {identity} has no title")
        description = row.get("description")
        if not isinstance(description, str) or len(compact_text(visible_text(description))) < 100:
            raise ValueError(f"Jupiter Keka job {identity} has an empty or short description")
        department_identifier = str(row.get("departmentIdentifier") or "")
        if not UUID.fullmatch(department_identifier) or not normalized_title(row.get("departmentName")):
            raise ValueError(f"Jupiter Keka job {identity} has no valid department identity")
        if departments is not None and department_identifier not in department_ids:
            raise ValueError(f"Jupiter Keka job {identity} department is absent from the portal department list")
        locations = row.get("jobLocations")
        if not isinstance(locations, list) or not locations:
            raise ValueError(f"Jupiter Keka job {identity} has no jobLocations")
        for location in locations:
            if not isinstance(location, dict) or not (normalized_title(location.get("name")) or normalized_title(location.get("city"))):
                raise ValueError(f"Jupiter Keka job {identity} has an unidentified job location")
        job_type = row.get("jobType")
        if job_type not in JOB_TYPE_LABELS:
            raise ValueError(f"Jupiter Keka job {identity} has an unknown jobType: {job_type!r}")
        if not isinstance(row.get("experience"), str):
            raise ValueError(f"Jupiter Keka job {identity} experience is not a string")
        _parse_published_on(row.get("publishedOn"), identity)
        if row.get("publishedSinceDays") is not None and not isinstance(row.get("publishedSinceDays"), int):
            issues.append(f"publishedSinceDays_not_integer:{identity}")
        if not isinstance(row.get("skillNames", []), list):
            issues.append(f"skillNames_not_list:{identity}")
        if row.get("salaryRange") is not None and not isinstance(row.get("salaryRange"), dict):
            issues.append(f"salaryRange_not_object:{identity}")
        if row.get("salaryRangeFormat") is not None and not isinstance(row.get("salaryRangeFormat"), str):
            issues.append(f"salaryRangeFormat_not_string:{identity}")
        rows.append(dict(row))
    return {"rows": rows, "ids": [str(row["id"]) for row in rows], "count": len(rows), "issues": issues}


def parse_job_detail_page(html: str, row: dict, expected_url: str) -> dict:
    parts = urlsplit(expected_url)
    match = JOB_DETAIL_PATH.fullmatch(parts.path)
    identity = str(row["id"])
    if (parts.scheme != "https" or parts.netloc != "jupiter.keka.com" or match is None
            or match.group(1) != identity):
        raise ValueError("Jupiter representative detail URL is not an exact exposed Keka job route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    h1 = soup.select_one("h1")
    actual_title = normalized_title(h1.get_text(" ", strip=True)) if h1 else ""
    expected_title = normalized_title(row.get("title"))
    if title != expected_title or actual_title != expected_title:
        raise ValueError(
            f"Jupiter exact detail {identity} title changed: page={title!r}/{actual_title!r} row={expected_title!r}"
        )
    container = soup.select_one(".job-description-container")
    if container is None:
        raise ValueError(f"Jupiter exact detail {identity} no longer exposes .job-description-container")
    description_html = container.decode_contents()
    description_text = visible_text(description_html)
    if len(compact_text(description_text)) < 100:
        raise ValueError(f"Jupiter exact detail {identity} description is empty or short")
    experience_node = soup.select_one(".ki-user-tie ~ span")
    location_node = soup.select_one(".ki-location ~ span")
    job_type_node = soup.select_one(".ki-briefcase ~ span")
    experience = normalized_title(experience_node.get_text(" ", strip=True)) if experience_node else ""
    location = normalized_title(location_node.get_text(" ", strip=True)) if location_node else ""
    job_type = normalized_title(job_type_node.get_text(" ", strip=True)) if job_type_node else ""
    expected_experience = normalized_title(row.get("experience"))
    expected_job_type = employment_type_label(row.get("jobType"))
    expected_locations = {
        normalized_title(location_row.get("name")) or normalized_title(location_row.get("city"))
        for location_row in row.get("jobLocations") or []
    }
    if experience != expected_experience:
        raise ValueError(f"Jupiter exact detail {identity} experience changed: {experience!r}")
    if location not in expected_locations:
        raise ValueError(f"Jupiter exact detail {identity} location is not in the listing: {location!r}")
    if job_type != expected_job_type:
        raise ValueError(f"Jupiter exact detail {identity} job type changed: {job_type!r}")
    social = soup.select_one(".social-links")
    selected_job_id = str(social.get("selectedjobid")) if social and social.get("selectedjobid") else None
    if selected_job_id != identity:
        raise ValueError(f"Jupiter exact detail {identity} selected-job identity changed: {selected_job_id!r}")
    other = soup.select_one(".other-openings-container")
    selected_department = str(other.get("selecteddepartmentidentifier")) if other and other.get("selecteddepartmentidentifier") else None
    if selected_department != str(row.get("departmentIdentifier")):
        raise ValueError(f"Jupiter exact detail {identity} selected department changed: {selected_department!r}")
    apply_button = soup.select_one("#apply-job")
    if apply_button is None or normalized_title(apply_button.get_text(" ", strip=True)) != "Apply for this job":
        raise ValueError(f"Jupiter exact detail {identity} apply button changed")
    if soup.select_one("form[name='jobApplicationForm']") is not None:
        raise ValueError(f"Jupiter exact detail {identity} unexpectedly contains the application form")
    description_soup = BeautifulSoup(description_html, "lxml")
    links = [
        {"text": normalized_title(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
        for anchor in description_soup.select("a[href]")
    ]
    return {
        "url": expected_url,
        "title": actual_title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_compact_chars": len(compact_text(description_text)),
        "description_fnv1a64": fnv1a64(description_text),
        "description_compact_sha256": sha256(compact_text(description_text).encode()).hexdigest(),
        "visible_experience": experience,
        "visible_location": location,
        "visible_job_type": job_type,
        "selected_job_id": selected_job_id,
        "selected_department_identifier": selected_department,
        "apply_button_text": normalized_title(apply_button.get_text(" ", strip=True)),
        "description_links": links,
        "role_attachments": [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")],
        "external_document_links": [
            link for link in links
            if re.search(r"drive\.google\.com/file/|docs\.google\.com/", link["url"] or "", re.I)
        ],
    }


def parse_apply_page(html: str, row: dict, expected_url: str) -> dict:
    parts = urlsplit(expected_url)
    match = APPLY_PATH.fullmatch(parts.path)
    identity = str(row["id"])
    if (parts.scheme != "https" or parts.netloc != "jupiter.keka.com" or match is None
            or match.group(1) != identity):
        raise ValueError("Jupiter apply URL is not an exact exposed Keka apply route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    expected_title = normalized_title(row.get("title"))
    if title != expected_title:
        raise ValueError(f"Jupiter apply form {identity} title changed: {title!r}")
    form = soup.select_one("form[name='jobApplicationForm']")
    if form is None:
        raise ValueError(f"Jupiter apply form {identity} no longer exposes jobApplicationForm")
    headings = [
        normalized_title(node.get_text(" ", strip=True))
        for node in soup.select("h1,h2")
        if normalized_title(node.get_text(" ", strip=True))
    ]
    if expected_title not in headings or "Apply for this job" not in headings:
        raise ValueError(f"Jupiter apply form {identity} headings changed: {headings!r}")
    file_inputs = [node.get("accept") for node in soup.select("input[type='file']")]
    return {
        "url": expected_url,
        "title": title,
        "form_name": form.get("name"),
        "headings": headings,
        "file_inputs": file_inputs,
        "application_form_fields_are_not_role_description": True,
    }


class Jupiter(Adapter):
    """Follow only Jupiter's official careers page, its exact Keka iframe, and that portal's feed."""

    VERIFICATION_REVISION = "jupiter.official-careers-keka-embed-active-details.1"

    @staticmethod
    def _assert_keka_url(url: str, path_prefix: str):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "jupiter.keka.com" or not parts.path.startswith(path_prefix):
            raise ValueError(f"Jupiter Keka request left the observed portal route: {url}")

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html,application/xhtml+xml"})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _keka_landing(self):
        started = time.perf_counter()
        response = await self.http.get(KEKA_CAREERS, headers={"Accept": "text/html,application/xhtml+xml"})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url).rstrip("/") != KEKA_CAREERS:
            raise ValueError("Jupiter Keka careers landing redirected to a different URL")
        parsed = parse_keka_landing(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _careerportal_document(self, url: str):
        started = time.perf_counter()
        response = await self.http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_careerportal_document(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _json(self, url: str, *, label: str):
        started = time.perf_counter()
        response = await self.http.get(url, headers={"Accept": "application/json", "Referer": KEKA_CAREERS})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"Jupiter {label} request redirected away from its observed route: {response.url}")
        return response.json(), {
            "url": url,
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        }

    async def _portal_metadata(self, domain: str, identifier: str):
        info, info_meta = await self._json(
            urljoin(domain, "api/organization/default/careerportalinfo"), label="careerportal info"
        )
        group, group_meta = await self._json(
            urljoin(domain, f"api/embedjobs/grouplinkstatus/{identifier}"), label="group link status"
        )
        departments, departments_meta = await self._json(
            urljoin(domain, f"api/embedjobs/departments/{identifier}"), label="departments"
        )
        return {
            "careerportal_info": parse_careerportal_info(info),
            "careerportal_info_request": info_meta,
            "group_link_status": parse_group_link_status(group),
            "group_link_status_request": group_meta,
            "departments": parse_departments(departments),
            "departments_request": departments_meta,
        }

    async def _active_snapshot(self, domain: str, identifier: str, departments: list[dict], *, label: str):
        url = urljoin(domain, f"api/embedjobs/default/active/{identifier}")
        started = time.perf_counter()
        response = await self.http.get(url, headers={"Accept": "application/json", "Referer": KEKA_CAREERS})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"Jupiter {label} active-job request redirected away from its observed route")
        parsed = parse_active_jobs(response.json(), departments)
        parsed.update(
            label=label,
            url=url,
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    def _make_job(self, row: dict, *, active_url: str, active_sha256: str):
        identity = str(row["id"])
        description_html = row["description"]
        description_text = visible_text(description_html)
        sections = description_sections(description_html)
        groups = grouped_sections(sections)
        responsibilities = _matching_groups(groups, RESPONSIBILITY_HEADING) or None
        qualifications = _matching_groups(groups, QUALIFICATION_HEADING) or None
        locations = [
            normalized_title(location.get("name")) or normalized_title(location.get("city"))
            for location in row["jobLocations"]
        ]
        job = self.job(
            {
                "listing": dict(row),
                "listing_api_url": active_url,
                "listing_api_sha256": active_sha256,
                "description_sections": sections,
                "description_groups": groups,
                "description_links": [
                    {"text": normalized_title(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
                    for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
                ],
                "role_attachments": [],
                "external_document_links": [
                    {"text": normalized_title(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
                    for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
                    if re.search(r"drive\.google\.com/file/|docs\.google\.com/", anchor.get("href") or "", re.I)
                ],
                "salary": {
                    "salaryRange": row.get("salaryRange"),
                    "salaryRangeFormat": row.get("salaryRangeFormat"),
                    "amount_range_exposed": False,
                    "note": "The portal exposes a currency/period placeholder with no minimum or maximum; no salary amount is published.",
                },
                "dates": {
                    "publishedOn": row.get("publishedOn"),
                    "publishedSinceDays": row.get("publishedSinceDays"),
                    "date_posted_mapping": "publishedOn is the source publication timestamp",
                    "date_updated": None,
                    "valid_through": None,
                },
                "employment_type_code": row.get("jobType"),
                "portal_name": EXPECTED_PORTAL_NAME,
                "legal_employer_exposed_per_job": False,
            },
            title=normalized_title(row["title"]),
            url=detail_url_for(identity),
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url_for(identity),
            description=description_html,
            method="jupiter.keka.embedjobs.active.description",
            description_url=active_url,
            locations=locations,
            department=normalized_title(row.get("departmentName")),
            team=None,
            employment_type=employment_type_label(row.get("jobType")),
            workplace_type=None,
            date_posted=row.get("publishedOn"),
            date_posted_raw={"publishedOn": row.get("publishedOn"),
                             "publishedSinceDays": row.get("publishedSinceDays")},
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=normalized_title(row.get("experience")),
            education=None,
            employer=None,
            language=None,
        )
        job.description_text = description_text
        job.record_identity = f"jupiter:{identity}"
        return job

    async def _detail(self, job):
        response = await self.http.get(
            job.url,
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": KEKA_CAREERS},
        )
        self._assert_keka_url(str(response.url), "/careers/jobdetails/")
        detail = parse_job_detail_page(response.text, job.raw["listing"], str(response.url))
        api_description = visible_text(job.raw["listing"].get("description"))
        matches_api = compact_text(api_description) == compact_text(detail["description_text"])
        job.raw["detail_page"] = {
            "source_url": str(response.url),
            "source_status": response.status_code,
            "source_response_sha256": sha256(response.content).hexdigest(),
            "source_response_bytes": len(response.content),
            "description_selector": ".job-description-container",
            "description_normalized_chars": detail["description_normalized_chars"],
            "description_compact_chars": detail["description_compact_chars"],
            "description_fnv1a64": detail["description_fnv1a64"],
            "description_compact_sha256": detail["description_compact_sha256"],
            "api_description_matches_detail_page": matches_api,
            "visible_experience": detail["visible_experience"],
            "visible_location": detail["visible_location"],
            "visible_job_type": detail["visible_job_type"],
            "selected_job_id": detail["selected_job_id"],
            "selected_department_identifier": detail["selected_department_identifier"],
            "apply_button_text": detail["apply_button_text"],
            "description_links": detail["description_links"],
            "role_attachments": detail["role_attachments"],
            "external_document_links": detail["external_document_links"],
            "application_form_present_on_detail_page": False,
        }
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = "jupiter.keka.jobdetails.description_container"
        job.description_source_url = str(response.url)
        job.apply_url = apply_url_for(job.job_id)
        job.employment_type = detail["visible_job_type"]
        job.experience = detail["visible_experience"]
        if not matches_api:
            job.warnings.append("listing_description_differs_from_detail_page")
            self.result.errors.append(f"jupiter_listing_detail_description_mismatch:{job.job_id}")
            self.result.listing_complete = False

        apply_url = apply_url_for(job.job_id)
        apply_started = time.perf_counter()
        try:
            apply_response = await self.http.get(
                apply_url,
                headers={"Accept": "text/html,application/xhtml+xml", "Referer": job.url},
            )
            apply_seconds = max(0.0, time.perf_counter() - apply_started)
            apply = parse_apply_page(apply_response.text, job.raw["listing"], str(apply_response.url))
            job.raw["apply_page"] = {
                **apply,
                "source_status": apply_response.status_code,
                "source_response_sha256": sha256(apply_response.content).hexdigest(),
                "source_response_bytes": len(apply_response.content),
                "seconds": apply_seconds,
            }
        except Exception as exc:
            job.raw["apply_page"] = {
                "url": apply_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.result.errors.append(f"Jupiter apply form {apply_url}: {type(exc).__name__}: {exc}")
        return job

    @staticmethod
    def _compare_rows(initial_rows: list[dict], final_rows: list[dict]) -> dict:
        initial_by_id = {str(row["id"]): row for row in initial_rows}
        final_by_id = {str(row["id"]): row for row in final_rows}
        changed_fields = {}
        volatile_fields = {}
        for identity in initial_by_id.keys() & final_by_id.keys():
            initial = initial_by_id[identity]
            final = final_by_id[identity]
            changed = sorted(
                key for key in set(initial) | set(final)
                if key != "publishedSinceDays" and initial.get(key) != final.get(key)
            )
            if changed:
                changed_fields[identity] = changed
            if initial.get("publishedSinceDays") != final.get("publishedSinceDays"):
                volatile_fields[identity] = {
                    "initial": initial.get("publishedSinceDays"),
                    "final": final.get("publishedSinceDays"),
                }
        return {
            "initial_ids": [str(row["id"]) for row in initial_rows],
            "final_ids": [str(row["id"]) for row in final_rows],
            "same_ids_and_order": [str(row["id"]) for row in initial_rows] == [str(row["id"]) for row in final_rows],
            "new_ids": [identity for identity in final_by_id if identity not in initial_by_id],
            "removed_ids": [identity for identity in initial_by_id if identity not in final_by_id],
            "changed_fields_by_id": changed_fields,
            "volatile_published_since_days": volatile_fields,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "keka_careers_url": KEKA_CAREERS,
            "listing_authority": (
                "The exact Keka active-job feed requested by the job-listing iframe published on "
                "Jupiter's own https://jupiter.money/careers/ page; the portal is not group-linked."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": "/careers/api/embedjobs/default/active/{identifier}",
                "identifier": EXPECTED_PORTAL_IDENTIFIER,
                "query_shape": None,
                "filters_applied": False,
                "scope": "jupiter.keka.com portal identifier only; no other Keka company or board is queried.",
            },
            "ui_filters": ["department", "location"],
            "pagination": "None: the active feed is a single list and the widget renders all rows; no next/previous/load-more controls were observed.",
            "detail_method": (
                "All exact /careers/jobdetails/{id} pages are fetched; their server-rendered "
                ".job-description-container is the primary description and the active-feed description is compared."
            ),
            "request_scope": "Only the exact jupiter.keka.com portal and the exact job/apply links it exposes.",
            "date_note": "date_posted uses the source publishedOn timestamp; date_updated and valid_through stay null because no such fields are published.",
            "salary_note": "No salary amount is published; the source salaryRange is a currency/period placeholder retained in raw.",
            "employer_note": "The portal identifies Jupiter Money; no per-job legal employer is published.",
        })
        try:
            entry = await self._official_entry()
            landing = await self._keka_landing()
            document = await self._careerportal_document(landing["portal_document_url"])
            if landing["portal_document_identifier"] != document["identifier"]:
                raise ValueError("Jupiter Keka careerportal document identifier does not match its khConfig")
            metadata = await self._portal_metadata(document["domain"], document["identifier"])
            if not self.page_guard():
                return self.result
            initial = await self._active_snapshot(
                document["domain"], document["identifier"], metadata["departments"], label="initial"
            )
        except Exception as exc:
            self.result.errors.append(f"Jupiter source collection failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        initial_source_seconds = max(0.0, time.perf_counter() - source_started)
        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["initial_keka_landing"] = landing
        self.result.metadata["initial_careerportal_document"] = document
        self.result.metadata["initial_portal_metadata"] = metadata
        self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.timings["keka_landing_seconds"] = landing["seconds"]
        self.result.timings["careerportal_document_seconds"] = document["seconds"]
        self.result.timings["portal_metadata_seconds"] = (
            metadata["careerportal_info_request"]["seconds"]
            + metadata["group_link_status_request"]["seconds"]
            + metadata["departments_request"]["seconds"]
        )
        self.result.timings["initial_listing_seconds"] = initial["seconds"]
        self.result.timings["initial_source_collection_seconds"] = initial_source_seconds
        self.result.expected_total = initial["count"]
        self.result.pages = 1
        self.result.errors.extend(initial["issues"])
        self.result.metadata["listing_url"] = initial["url"]
        self.result.metadata["portal_identifier"] = document["identifier"]
        self.result.metadata["portal_domain"] = document["domain"]
        self.result.metadata["portal_document_url"] = landing["portal_document_url"]
        self.result.metadata["initial_api_count"] = initial["count"]

        if initial["issues"]:
            self.result.errors.append("jupiter_initial_listing_schema_issues")
            self.result.jobs = [
                self._make_job(row, active_url=initial["url"], active_sha256=initial["response_sha256"])
                for row in initial["rows"]
            ]
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        jobs = [
            self._make_job(row, active_url=initial["url"], active_sha256=initial["response_sha256"])
            for row in initial["rows"]
        ]
        self.result.jobs = jobs
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.timings["detail_hydration_seconds"] = self.result.timings.get("detail_phase_seconds", 0.0)

        final_started = time.perf_counter()
        try:
            final_entry = await self._official_entry()
            final_landing = await self._keka_landing()
            final_document = await self._careerportal_document(final_landing["portal_document_url"])
            if final_landing["portal_document_identifier"] != final_document["identifier"]:
                raise ValueError("Jupiter final Keka careerportal identifier does not match its khConfig")
            final_metadata = await self._portal_metadata(final_document["domain"], final_document["identifier"])
            final = await self._active_snapshot(
                final_document["domain"], final_document["identifier"], final_metadata["departments"], label="final"
            )
        except Exception as exc:
            self.result.errors.append(f"Jupiter final source recheck failed: {type(exc).__name__}: {exc}")
            final_entry = final_landing = final_document = final_metadata = final = None
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        if final is None:
            self.result.metadata["membership_reconciliation"] = {"complete": False, "reason": "final_recheck_failed"}
            self.result.listing_complete = False
        else:
            comparison = self._compare_rows(initial["rows"], final["rows"])
            same_portal = (
                final_entry["iframe_url"] == entry["iframe_url"] == KEKA_CAREERS
                and final_entry["view_all_openings"] == entry["view_all_openings"]
                and final_landing["portal_document_url"] == landing["portal_document_url"]
                and final_document["identifier"] == document["identifier"]
                and final_document["domain"] == document["domain"]
                and final_metadata["careerportal_info"] == metadata["careerportal_info"]
                and final_metadata["group_link_status"] == metadata["group_link_status"]
                and final_metadata["departments"] == metadata["departments"]
            )
            counts_match = initial["count"] == final["count"]
            complete = (
                comparison["same_ids_and_order"]
                and not comparison["new_ids"]
                and not comparison["removed_ids"]
                and not comparison["changed_fields_by_id"]
                and counts_match
                and same_portal
                and not final["issues"]
                and len(self.result.jobs) == initial["count"]
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_keka_landing"] = final_landing
            self.result.metadata["final_careerportal_document"] = final_document
            self.result.metadata["final_portal_metadata"] = final_metadata
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                **comparison,
                "initial_count": initial["count"],
                "final_count": final["count"],
                "counts_match": counts_match,
                "same_portal_identity_and_metadata": same_portal,
                "initial_sha256": initial["response_sha256"],
                "final_sha256": final["response_sha256"],
                "complete": complete,
            }
            if not same_portal:
                self.result.errors.append("jupiter_official_embed_or_portal_metadata_changed_during_collection")
            if not comparison["same_ids_and_order"] or comparison["new_ids"] or comparison["removed_ids"]:
                self.result.errors.append("jupiter_listing_membership_changed_during_collection")
            if comparison["changed_fields_by_id"]:
                self.result.errors.append("jupiter_source_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])
            self.result.listing_complete = bool(complete and not self.result.errors)

        self.result.metadata["job_ids"] = [str(job.job_id) for job in self.result.jobs]
        self.result.metadata["api_meta_count"] = initial["count"]
        self.result.metadata["detail_hydration_mode"] = (
            "Every exact jobdetail page was fetched; the server-rendered description is the primary source."
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["source_collection_wall_seconds"] = self.result.metadata["source_collection_wall_seconds"]
        return self.result
