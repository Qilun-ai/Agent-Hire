"""Shopify's own careers listing and its published React Router job-posting data."""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.shopify.com/careers"
LISTING_URL = "https://www.shopify.com/careers/jobs"
LISTING_HOST = "www.shopify.com"
LISTING_PATH = "/careers/jobs"
OFFICIAL_PATH = "/careers"
DETAIL_PATH = re.compile(
    r"^/careers/[a-z0-9][a-z0-9-]*_"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
ROUTE_OFFICIAL = "($locale)/careers"
ROUTE_LISTING = "($locale)/careers/jobs"
ROUTE_POSTING = "($locale)/careers/$posting"
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SITE_LEVEL_DETAIL_SECTIONS = ("job-discipline", "job-compensation", "job-life", "job-applying", "job-apply")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you will do", "what you’ll do",
    "what you will be doing", "what you'll own", "what you will own",
    "key responsibilities", "your impact",
)
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "what you'll bring", "what you will bring",
    "what we're looking for", "what we’re looking for", "what you'll need",
    "what you will need", "what you need", "who you are", "about you",
    "nice to have", "nice to haves", "bonus", "experience",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def rendered_text(html: str | None) -> str:
    """Collapse visible text exactly as the browser text node would appear."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def words_only(value: str | None) -> str:
    """Punctuation-free comparison key for employer source vs rendered body.

    Shopify's richtext renderer turns a bold-only paragraph such as
    ``<p><strong>Key Responsibilities:</strong></p>`` into ``<h2>Key
    Responsibilities</h2>``. The heading colon is therefore present in the
    source HTML but absent from the rendered DOM; word order is unchanged.
    """
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _decode_router_payload(html: str) -> dict:
    """Decode React Router's turbo-stream payload into a normal Python object."""
    soup = BeautifulSoup(html, "lxml")
    scripts = [
        script.string for script in soup.find_all("script")
        if script.string and "streamController.enqueue" in script.string
    ]
    if not scripts:
        raise ValueError("Shopify page has no React Router stream payload")
    chunks = []
    for script in scripts:
        for literal in re.findall(r'streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)', script, re.S):
            chunks.append(json.loads(literal))
    if not chunks:
        raise ValueError("Shopify page stream payload has no enqueue chunks")
    payload = json.loads("".join(chunks))
    if not isinstance(payload, list) or not payload:
        raise ValueError("Shopify React Router payload is not a non-empty array")

    memo: dict[int, object] = {}

    def decode_reference(index: int, stack: frozenset[int]):
        if index < 0:
            return None
        if index in stack:
            return "<cycle>"
        if index in memo:
            return memo[index]
        value = decode_value(payload[index], stack | {index})
        memo[index] = value
        return value

    def decode_value(value, stack=frozenset()):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = decode_reference(int(key[1:]), stack) if key.startswith("_") else key
                result[str(name)] = decode_value(item, stack)
            return result
        if isinstance(value, list):
            return [decode_value(item, stack) for item in value]
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return decode_reference(value, stack)
        return value

    root = decode_reference(0, frozenset())
    if not isinstance(root, dict):
        raise ValueError("Shopify React Router payload root changed")
    return root


def _route(root: dict, route: str) -> dict:
    loader_data = root.get("loaderData")
    if not isinstance(loader_data, dict) or route not in loader_data:
        raise ValueError(f"Shopify React Router payload is missing route {route}")
    data = loader_data[route]
    if not isinstance(data, dict):
        raise ValueError(f"Shopify route {route} payload is not an object")
    return data


def _check_url(response_url: str, path: str, *, detail_id: str | None = None) -> str:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != LISTING_HOST:
        raise ValueError(f"Shopify response came from an unexpected host: {response_url}")
    if path == LISTING_PATH:
        if parsed.path.rstrip("/") != LISTING_PATH or parsed.query:
            raise ValueError(f"Shopify open-roles listing URL changed: {response_url}")
    elif path == OFFICIAL_PATH:
        if parsed.path.rstrip("/") != OFFICIAL_PATH or parsed.query:
            raise ValueError(f"Shopify official careers entry URL changed: {response_url}")
    else:
        match = DETAIL_PATH.fullmatch(parsed.path)
        if match is None or parsed.query:
            raise ValueError(f"Shopify job detail URL changed: {response_url}")
        if detail_id is not None and match[1] != detail_id:
            raise ValueError("Shopify job detail URL does not identify the listed posting")
    return response_url


def _role_rows(roles) -> list[dict]:
    rows, issues, seen = [], [], set()
    for role in roles:
        if not isinstance(role, dict):
            issues.append("shopify_listing_role_not_object")
            continue
        identity = str(role.get("id", ""))
        href = role.get("href") or ""
        match = DETAIL_PATH.fullmatch(urlsplit(href).path) if href.startswith("/careers/") else None
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", identity):
            issues.append("shopify_listing_role_missing_uuid")
            continue
        if identity in seen:
            issues.append(f"shopify_listing_duplicate_role_id:{identity}")
            continue
        if match is None or match[1] != identity or urlsplit(href).query:
            issues.append(f"shopify_listing_role_href_mismatch:{identity}")
            continue
        title = normalized_title(role.get("title"))
        locations = role.get("locations")
        if (not title or not isinstance(role.get("location"), str)
                or not isinstance(locations, list) or not locations
                or any(not isinstance(value, str) or not value.strip() for value in locations)
                or not normalized_title(role.get("discipline"))):
            issues.append(f"shopify_listing_role_fields_missing:{identity}")
            continue
        seen.add(identity)
        rows.append(dict(role))
    return rows, issues


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _check_url(response_url, OFFICIAL_PATH)
    root = _decode_router_payload(html)
    data = _route(root, ROUTE_OFFICIAL)
    roles, issues = _role_rows(data.get("roles"))
    if data.get("jobsHref") != "/careers/jobs":
        issues.append("shopify_official_jobs_href_changed")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Shopify" not in title:
        issues.append("shopify_official_page_title_changed")
    rendered_rows = soup.select('[data-component-name="job-row"]')
    load_more = [
        button.get_text(" ", strip=True) for button in soup.select("button")
        if re.fullmatch(r"load more", button.get_text(" ", strip=True), re.I)
    ]
    status_text = [node.get_text(" ", strip=True) for node in soup.select('p[role="status"]')]
    rendered_count = next(
        (int(match.group(1)) for text in status_text if (match := re.search(r"(\d+)\s+roles?", text))),
        None,
    )
    quest_groups = [
        node.get_text(" ", strip=True)
        for node in soup.select('[data-component-name="quest-group"]')
    ]
    if rendered_count is not None and rendered_count != len(roles):
        issues.append("shopify_official_rendered_count_mismatch")
    if not roles:
        issues.append("shopify_official_listing_has_no_roles")
    return {
        "url": response_url,
        "title": title,
        "jobs_href": data.get("jobsHref"),
        "roles": roles,
        "role_ids": [str(role["id"]) for role in roles],
        "count": len(roles),
        "rendered_count": rendered_count,
        "status_text": status_text,
        "rendered_rows": len(rendered_rows),
        "load_more_controls": load_more,
        "quest_group_labels": quest_groups,
        "issues": issues,
    }


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    _check_url(response_url, LISTING_PATH)
    root = _decode_router_payload(html)
    data = _route(root, ROUTE_LISTING)
    roles, issues = _role_rows(data.get("roles"))
    board = data.get("jobsBoard") if isinstance(data.get("jobsBoard"), dict) else {}
    if board.get("metaTitle") != "Open roles at Shopify":
        issues.append("shopify_listing_board_identity_changed")

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title != "Open roles at Shopify":
        issues.append("shopify_listing_page_title_changed")
    status_text = [node.get_text(" ", strip=True) for node in soup.select('p[role="status"]')]
    displayed_count = next(
        (int(match.group(1)) for text in status_text if (match := re.search(r"(\d+)\s+roles?", text))),
        None,
    )
    dom_rows = soup.select('[data-component-name="job-row"]')
    dom = []
    for row in dom_rows:
        href = row.get("href") or ""
        match = DETAIL_PATH.fullmatch(urlsplit(href).path)
        heading = row.find("h3")
        location = row.find("p", id=lambda value: value and value.endswith("-location"))
        dom.append({
            "href": href,
            "id": (row.get("data-component-extra-job-posting-id") or ""),
            "uuid": match[1] if match else None,
            "title": normalized_title(heading.get_text(" ", strip=True)) if heading else "",
            "location": normalized_title(location.get_text(" ", strip=True)) if location else "",
            "discipline": normalized_title(row.get("data-component-extra-discipline")),
        })
    if len(dom) != len(roles):
        issues.append("shopify_listing_rendered_row_count_mismatch")
    elif displayed_count is not None and displayed_count != len(roles):
        issues.append("shopify_listing_displayed_count_mismatch")
    for index, (role, row) in enumerate(zip(roles, dom)):
        identity = str(role["id"])
        if row["id"] != identity or row["uuid"] != identity:
            issues.append(f"shopify_listing_dom_row_identity_mismatch:{identity}")
        elif (row["title"] != normalized_title(role.get("title"))
              or row["location"] != normalized_title(role.get("location"))
              or row["discipline"] != normalized_title(role.get("discipline"))):
            issues.append(f"shopify_listing_dom_row_field_mismatch:{identity}")
        if index and dom[index - 1]["id"] == identity:
            issues.append(f"shopify_listing_repeated_dom_row:{identity}")
    load_more = [
        button.get_text(" ", strip=True) for button in soup.select("button")
        if re.fullmatch(r"load more", button.get_text(" ", strip=True), re.I)
    ]
    if load_more:
        issues.append("shopify_listing_now_uses_load_more_pagination")
    location_controls = [
        node.get("data-component-extra-location") or node.get_text(" ", strip=True)
        for node in soup.select('[data-component-name="jobs-filter-location"]')
    ]
    location_controls = [value for value in location_controls if value]
    discipline_controls = [
        node.get_text(" ", strip=True)
        for node in soup.select('[data-component-name="jobs-filter-discipline"]')
    ]
    if not roles:
        issues.append("shopify_listing_has_no_roles")
    return {
        "url": response_url,
        "title": title,
        "roles": roles,
        "job_ids": [str(role["id"]) for role in roles],
        "count": len(roles),
        "displayed_count": displayed_count,
        "status_text": status_text,
        "rendered_rows": len(dom_rows),
        "dom_rows": dom,
        "pagination_controls": load_more,
        "location_filter_controls": location_controls,
        "discipline_filter_controls": discipline_controls,
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-shopify-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-shopify-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find(["strong", "b"], recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and 0 < len(full_text) <= 160:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading.rstrip(":").strip() or heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _matching_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    matches = []
    for section in sections:
        heading = (section.get("heading") or "").lower().replace("’", "'")
        if heading and any(token in heading for token in tokens):
            matches.append(section)
    return matches or None


def _clean_job_record(job: dict) -> dict:
    """Keep public posting fields but drop applicant data and hiring contacts."""
    cleaned = {
        key: value for key, value in job.items()
        if key not in {"hiringTeam", "author"}
    }
    return cleaned


def parse_detail(html: str, expected_role: dict, response_url: str) -> dict:
    identity = str(expected_role["id"])
    _check_url(response_url, "detail", detail_id=identity)
    root = _decode_router_payload(html)
    data = _route(root, ROUTE_POSTING)
    posting = data.get("jobPosting")
    job = data.get("job")
    if not isinstance(posting, dict) or not isinstance(job, dict):
        raise ValueError("Shopify detail page is missing its job posting object")
    issues = []
    if str(posting.get("id")) != identity:
        raise ValueError("Shopify detail posting id does not match the listed role")
    if posting.get("status") != "Published":
        issues.append("shopify_detail_posting_not_published")
    if posting.get("isListed") is not True:
        issues.append("shopify_detail_posting_not_listed")
    associations = posting.get("jobBoardAssociations")
    if isinstance(associations, list) and associations:
        if not any(item.get("isListed") is True for item in associations if isinstance(item, dict)):
            issues.append("shopify_detail_posting_not_on_a_public_board")

    description_html = posting.get("descriptionHtml") or ""
    description_plain = posting.get("descriptionPlain") or ""
    if not description_html.strip():
        raise ValueError("Shopify detail page has an empty descriptionHtml")
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError("Shopify detail description is shorter than a complete role body")

    canonical = data.get("canonicalUrl") or response_url
    _check_url(canonical, "detail", detail_id=identity)
    if canonical != response_url:
        issues.append("shopify_detail_canonical_url_differs_from_response")

    soup = BeautifulSoup(html, "lxml")
    article = soup.select_one('article[itemtype="https://schema.org/JobPosting"]')
    if article is None:
        raise ValueError("Shopify detail page has no JobPosting article")
    if article.get("data-component-extra-job-posting-id") != identity:
        raise ValueError("Shopify detail article posting id does not match the listed role")
    heading = article.find("h1")
    public_title = normalized_title(heading.get_text(" ", strip=True)) if heading else ""
    if words_only(public_title) != words_only(expected_role.get("title")):
        raise ValueError("Shopify detail title does not match the listed role")
    header = article.select_one("header p")
    rendered_location = normalized_title(header.get_text(" ", strip=True)) if header else ""
    if words_only(rendered_location) != words_only(expected_role.get("location")):
        issues.append("shopify_detail_header_location_differs_from_listing")
    body = article.select_one('[itemprop="description"]')
    if body is None:
        raise ValueError("Shopify detail page has no rendered description container")
    rendered_body = rendered_text(str(body))
    source_words = words_only(description_text)
    rendered_words = words_only(rendered_body)
    if source_words != rendered_words:
        raise ValueError("Shopify rendered description body does not match descriptionHtml")
    sections = description_sections(description_html)
    description_soup = BeautifulSoup(description_html, "lxml")
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in description_soup.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]

    published = posting.get("publishedDate")
    if not isinstance(published, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
        raise ValueError("Shopify detail page has no ISO publishedDate")
    deadline = posting.get("applicationDeadline")
    compensation = posting.get("compensation")
    salary = None
    if isinstance(compensation, dict) and compensation.get("shouldDisplayCompensation") is True:
        salary = compensation

    locations_source = data.get("locations") if isinstance(data.get("locations"), list) else []
    location_by_id = {
        str(item.get("id")): item for item in locations_source
        if isinstance(item, dict) and item.get("id")
    }
    location_ids = posting.get("locationIds") if isinstance(posting.get("locationIds"), dict) else {}
    primary = location_ids.get("primaryLocationId")
    secondary = location_ids.get("secondaryLocationIds") or []
    ordered_ids = ([primary] if primary else []) + list(secondary)
    listing_locations = list(expected_role.get("locations") or [])
    locations = []
    for index, location_id in enumerate(ordered_ids):
        source = location_by_id.get(str(location_id))
        display = listing_locations[index] if index < len(listing_locations) else None
        if source is None:
            issues.append(f"shopify_detail_location_id_unresolved:{location_id}")
            if display:
                locations.append({"name": display, "location_id": str(location_id)})
            continue
        source_name = normalized_title(source.get("name"))
        locations.append({
            "name": display or source_name,
            "source_name": source_name,
            "location_id": str(location_id),
            "workplace_type": source.get("workplaceType") or posting.get("workplaceType"),
            "is_remote": source.get("isRemote"),
            "address": source.get("address"),
        })
        display_suffix = display.split(" - ", 1)[-1] if display else ""
        if display and words_only(display_suffix) not in words_only(source_name):
            issues.append(f"shopify_detail_location_name_mismatch:{location_id}")
    if len(ordered_ids) != len(listing_locations):
        issues.append("shopify_detail_location_count_differs_from_listing")
    if not locations and listing_locations:
        locations = [{"name": name} for name in listing_locations]

    published_words = words_only(description_text)
    rendered_words = words_only(rendered_body)
    audit = {
        "rendered_body_normalized_chars": len(rendered_body),
        "rendered_body_fnv1a64": fnv1a64(rendered_body),
        "description_source_normalized_chars": len(description_text),
        "description_source_fnv1a64": fnv1a64(description_text),
        "rendered_body_words_chars": len(rendered_words),
        "rendered_body_words_fnv1a64": fnv1a64(rendered_words),
        "description_source_words_chars": len(published_words),
        "description_source_words_fnv1a64": fnv1a64(published_words),
        "rendered_body_matches_description_source": True,
        "description_plain_sha256": sha256(description_plain.encode()).hexdigest() if description_plain else None,
        "description_plain_chars": len(description_plain),
        "excluded_site_sections": list(SITE_LEVEL_DETAIL_SECTIONS),
        "personal_data_excluded": ["hiringTeam", "author", "applicationFormDefinition", "surveyFormDefinitions"],
    }
    return {
        "url": canonical,
        "title": public_title,
        "posting_id": identity,
        "job_record": _clean_job_record(job),
        "job_posting": {
            key: value for key, value in posting.items()
            if key not in {
                "descriptionHtml", "descriptionPlain", "descriptionParts",
                "applicationFormDefinition", "surveyFormDefinitions",
            }
        },
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": sections,
        "responsibilities": _matching_sections(sections, RESPONSIBILITY_TOKENS),
        "qualifications": _matching_sections(sections, QUALIFICATION_TOKENS),
        "description_links": links,
        "role_attachments": attachments,
        "locations": locations,
        "published_date": published,
        "valid_through": deadline,
        "salary": salary,
        "compensation": compensation,
        "audit": audit,
        "issues": issues,
    }


class Shopify(Adapter):
    """Read only Shopify's own careers listing and its exact published detail routes."""

    VERIFICATION_REVISION = "shopify.careers-react-router.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.blocked_reason: str | None = None

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _detail(self, job):
        if self.blocked_reason:
            reason = f"detail requests stopped: {self.blocked_reason}"
            job.raw["detail_failure"] = f"RuntimeError: {reason}"
            raise RuntimeError(reason)
        fetch_started = time.perf_counter()
        try:
            response = await self.http.get(job.url)
        except FetchError as exc:
            message = str(exc)
            if any(marker in message for marker in ("HTTP 429", "HTTP 403", "HTTP 401", "captcha", "CAPTCHA")):
                self.blocked_reason = message
            job.raw["detail_failure"] = f"{type(exc).__name__}: {message}"
            raise
        fetch_seconds = max(0.0, time.perf_counter() - fetch_started)
        parse_started = time.perf_counter()
        try:
            parsed = parse_detail(response.text, job.raw["listing"], str(response.url))
        except Exception as exc:
            job.raw["detail_failure"] = f"{type(exc).__name__}: {exc}"
            raise
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        self.result.timings["detail_http_seconds_sum"] = (
            self.result.timings.get("detail_http_seconds_sum", 0.0) + fetch_seconds
        )
        self.result.timings["detail_parse_seconds_sum"] = (
            self.result.timings.get("detail_parse_seconds_sum", 0.0) + parse_seconds
        )
        job.raw["detail_http_seconds"] = fetch_seconds
        job.raw["detail_parse_seconds"] = parse_seconds
        return self._make_job(job.raw["listing"], parsed)

    def _preliminary_job(self, role):
        return self.job(
            {"listing": role},
            title=normalized_title(role.get("title")),
            url=f"https://{LISTING_HOST}{role['href']}",
            job_id=str(role["id"]),
            locations=list(role.get("locations") or []),
            department=normalized_title(role.get("discipline")),
        )

    def _make_job(self, role, detail):
        posting = detail["job_posting"]
        published = detail["published_date"]
        job = self.job(
            {
                "listing": role,
                "detail": {
                    "posting_id": detail["posting_id"],
                    "job_posting": posting,
                    "job_record": detail["job_record"],
                    "published_date": detail["published_date"],
                    "application_deadline": detail["valid_through"],
                    "compensation": detail["compensation"],
                    "locations": detail["locations"],
                    "custom_fields": posting.get("customFields"),
                    "job_board_associations": posting.get("jobBoardAssociations"),
                    "linked_data": posting.get("linkedData"),
                    "description_sections": detail["description_sections"],
                    "description_links": detail["description_links"],
                    "role_attachments": detail["role_attachments"],
                    "audit": detail["audit"],
                    "listing_issues": detail["issues"],
                    "excluded_source_fields": [
                        "descriptionPlain (link URLs differ from visible HTML)",
                        "hiringTeam/author personal contacts",
                        "applicationFormDefinition/surveyFormDefinitions",
                    ],
                    "site_level_sections_not_description": list(SITE_LEVEL_DETAIL_SECTIONS),
                    "apply_control": {
                        "detail_anchor": "#apply",
                        "page_apply_link": posting.get("applyLink"),
                        "external_link": posting.get("externalLink"),
                    },
                },
            },
            title=detail["title"],
            url=detail["url"],
            job_id=detail["posting_id"],
            requisition_id=detail["job_record"].get("customRequisitionId"),
            apply_url=posting.get("applyLink") or detail["url"],
            description=detail["description_html"],
            method="shopify.careers.react-router.jobPosting.descriptionHtml",
            description_url=detail["url"],
            locations=detail["locations"],
            department=normalized_title(posting.get("departmentName")),
            team=normalized_title(posting.get("teamName")),
            employment_type=posting.get("employmentType"),
            workplace_type=posting.get("workplaceType"),
            date_posted=published,
            date_posted_raw={
                "publishedDate": published,
                "linkedData.datePosted": (posting.get("linkedData") or {}).get("datePosted"),
                "createdAt": detail["job_record"].get("createdAt"),
                "openedAt": detail["job_record"].get("openedAt"),
                "updatedAt": detail["job_record"].get("updatedAt"),
                "postingUpdatedAt": posting.get("updatedAt"),
            },
            date_updated=None,
            valid_through=detail["valid_through"],
            salary=detail["salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            employer="Shopify",
            language="en",
        )
        job.description_text = detail["description_text"]
        for issue in detail["issues"]:
            job.warnings.append(issue)
        if job.raw["detail"]["role_attachments"]:
            job.warnings.append("role_attachments_require_exact_review")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "Shopify's own Open roles page; the careers entry payload publishes jobsHref=/careers/jobs and carries the same 115 posting ids.",
            "listing_membership_request": {
                "method": "GET",
                "route": LISTING_PATH,
                "encoding": "server-rendered React Router turbo-stream loaderData",
                "filters_applied": False,
                "pagination_request": None,
                "scope": "Only www.shopify.com/careers/jobs roles; no Ashby board or other company is enumerated.",
            },
            "detail_method": "Each /careers/<slug>_<uuid> page's React Router jobPosting.descriptionHtml is verified against the rendered [itemprop=description] article body.",
            "request_scope": "Exact posting links published by Shopify's official listing; the ?ashby_jid application link is retained as a source field, not used for discovery.",
        })
        try:
            official_started = time.perf_counter()
            official = await self._official_entry()
            listing_started = time.perf_counter()
            listing = await self._listing()
            listing_finished = time.perf_counter()
            self.result.timings["official_entry_seconds"] = max(0.0, listing_started - official_started)
            self.result.timings["listing_parse_seconds"] = max(0.0, listing_finished - listing_started)
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        except Exception as exc:
            self.result.errors.append(f"Shopify source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_official_entry"] = {
            key: value for key, value in official.items() if key != "roles"
        }
        self.result.metadata["initial_official_role_ids"] = official["role_ids"]
        self.result.metadata["initial_listing"] = {
            key: value for key, value in listing.items() if key not in {"roles", "dom_rows"}
        }
        self.result.metadata["initial_listing_dom_rows"] = listing["dom_rows"]
        if official["issues"]:
            self.result.errors.extend(official["issues"])
        if listing["issues"]:
            self.result.errors.extend(listing["issues"])
        if official["role_ids"] != listing["job_ids"]:
            self.result.errors.append("shopify_official_entry_and_open_roles_listing_membership_mismatch")
        if listing["displayed_count"] != listing["count"]:
            self.result.errors.append("shopify_listing_displayed_count_differs_from_roles")

        self.result.expected_total = listing["displayed_count"] or listing["count"]
        jobs = [self._preliminary_job(role) for role in listing["roles"]]
        try:
            hydrated = await self.hydrate(jobs, self._detail)
        except Exception as exc:
            self.result.errors.append(f"Shopify detail phase failed: {type(exc).__name__}: {exc}")
            hydrated = jobs
        self.result.jobs = list(hydrated)

        retained_ids = [str(job.job_id) for job in self.result.jobs]
        if retained_ids != listing["job_ids"]:
            self.result.errors.append("shopify_detail_records_do_not_match_listing_ids")
        failed = [
            {
                "id": str(job.job_id),
                "url": job.url,
                "reason": (job.raw.get("detail_failure") or "description not verified"),
            }
            for job in self.result.jobs if not job.description_verified
        ]
        if failed:
            self.result.metadata["detail_failures"] = failed
            for item in failed:
                self.result.errors.append(f"shopify_detail_failed:{item['id']}:{item['reason']}")
        if self.blocked_reason:
            self.result.metadata["blocked_reason"] = self.blocked_reason
            self.result.errors.append(f"shopify_detail_requests_blocked:{self.blocked_reason}")

        final_started = time.perf_counter()
        try:
            final = await self._listing()
        except Exception as exc:
            final = None
            self.result.errors.append(f"Shopify final listing recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_listing_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)
        self.result.metadata["final_listing"] = (
            {key: value for key, value in final.items() if key not in {"roles", "dom_rows"}}
            if final is not None else None
        )
        if final is not None:
            if final["issues"]:
                self.result.errors.extend(final["issues"])
            initial_by_id = {row["id"]: row for row in listing["dom_rows"]}
            final_by_id = {row["id"]: row for row in final["dom_rows"]}
            changed_rows = {
                identity: sorted(
                    key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                    if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                )
                for identity in initial_by_id
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]
            }
            same_ids = final["job_ids"] == listing["job_ids"]
            same_count = (final["count"], final["displayed_count"]) == (
                listing["count"], listing["displayed_count"]
            )
            if not same_ids or not same_count:
                self.result.errors.append("shopify_listing_membership_changed_during_collection")
            if changed_rows:
                self.result.errors.append("shopify_listing_fields_changed_during_collection")
            reconciled = same_ids and same_count and not changed_rows and not final["issues"]
        else:
            changed_rows = {}
            reconciled = False
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": listing["job_ids"],
            "final_ids": final["job_ids"] if final is not None else None,
            "initial_count": listing["count"],
            "final_count": final["count"] if final is not None else None,
            "initial_displayed_count": listing["displayed_count"],
            "final_displayed_count": final["displayed_count"] if final is not None else None,
            "initial_rendered_rows": listing["rendered_rows"],
            "final_rendered_rows": final["rendered_rows"] if final is not None else None,
            "same_ids_and_order": bool(final is not None and final["job_ids"] == listing["job_ids"]),
            "all_row_fields_match": not changed_rows,
            "changed_fields_by_job_id": changed_rows,
            "new_job_ids": [identity for identity in (final["job_ids"] if final is not None else [])
                            if identity not in listing["job_ids"]],
            "removed_job_ids": [identity for identity in listing["job_ids"]
                                if final is not None and identity not in final["job_ids"]],
            "complete": reconciled,
        }
        self.result.metadata["home_quest_count_discrepancy"] = {
            "home_quest_group_labels": official.get("quest_group_labels"),
            "open_roles_listing_count": listing["count"],
            "note": "The home career page's three quest labels (56+17+54) are stale marketing counts; the Open roles page and both payloads contain the same 115 unique postings, and the listing itself displays 115 roles.",
        }
        self.result.metadata["job_ids"] = retained_ids
        self.result.metadata["listing_total"] = listing["count"]
        self.result.metadata["job_count"] = len(self.result.jobs)
        self.result.metadata["descriptions_verified"] = sum(
            job.description_verified for job in self.result.jobs
        )
        self.result.listing_complete = bool(reconciled and len(self.result.jobs) == listing["count"])
        self.result.timings["adapter_total_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
