"""Kaleyra's company-scoped careers board (FreshTeam) and its official route.

The ledger URL ``https://www.kaleyra.com/careers/`` currently 301-redirects to
the Tata Communications parent careers portal
(``https://jobs.tatacommunications.com/home``).  That portal is a parent-wide
workspace (``TCLPROD-c62po``, 244 roles, no Kaleyra company/brand facet); it is
rejected as a Kaleyra listing.

Kaleyra's own ``careers.kaleyra.com`` hostname is a CNAME to
``kaleyra-talent.freshteam.com``.  The FreshTeam board there is branded with
Kaleyra's site assets, publishes ``Technology - 3 Open Roles``, and every role's
JSON-LD has ``hiringOrganization.name = "Kaleyra"``.  The custom hostname is
currently broken (TLS handshake failure; HTTP 409 Cloudflare 1001), so the
native FreshTeam host is the same board's working origin.  This adapter follows
only that company-scoped board, collects its listing membership and full
descriptions, and records the broken custom host and the rejected parent board
as evidence.
"""
from __future__ import annotations

import html as html_module
import json
import re
import socket
import time
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from ..http import FetchError
from ..models import text_html
from .base import Adapter


OFFICIAL_ENTRY = "https://www.kaleyra.com/careers/"
PARENT_PORTAL = "https://jobs.tatacommunications.com/home"
CAREERS_SUBDOMAIN_HOST = "careers.kaleyra.com"
CUSTOM_HOST_URL = "https://careers.kaleyra.com/jobs"
LISTING_URL = "https://kaleyra-talent.freshteam.com/jobs"
LISTING_HOST = "kaleyra-talent.freshteam.com"
LISTING_PATH = "/jobs"
EXPECTED_EMPLOYER = "Kaleyra"
JOB_PATH = re.compile(r"^/jobs/([A-Za-z0-9]+)/([a-z0-9-]+)$")
REQUISITION = re.compile(r"#(\d+)")
ROLE_COUNT = re.compile(r"(\d+)\s+Open Roles?", re.I)
HIRING_HEADINGS = (
    "what you will do", "what you'll do", "responsibilities",
    "key responsibilities", "role and responsibilities", "your role",
)
QUALIFICATION_HEADINGS = (
    "skill", "requirement", "qualification", "experience",
    "who you are", "what we are looking for", "expected skill",
)
PAGINATION = re.compile(r"\b(?:next|previous|prev|load more|show more)\b", re.I)
CHANGED = "kaleyra_official_journey_changed_requires_individual_membership_review"
CNAME_CHANGED = "kaleyra_official_careers_subdomain_no_longer_delegates_to_reviewed_freshteam_board"
CNAME_FAILED = "kaleyra_official_careers_subdomain_resolution_failed_scope_unverified"
VISIBLE_MISMATCH = "kaleyra_detail_visible_description_does_not_match_jsonld_job_posting"


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value or "")).strip()


def normalized_body(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def compact_sha256(value: str) -> str:
    return sha256(re.sub(r"\s+", "", value or "").encode()).hexdigest()


def parse_date_posted(raw: str | None) -> str | None:
    """Convert FreshTeam's public `datePosted` string to ISO-8601 UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.endswith("UTC"):
            parsed = parsed.replace(tzinfo=timezone.utc)
        elif parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def _count_open_roles(soup: BeautifulSoup) -> tuple[int | None, list[str]]:
    labels = [
        normalize_text(node.get_text(" ", strip=True))
        for node in soup.select(".role-count, .mobile-role-count")
    ]
    values = {int(match.group(1)) for label in labels for match in ROLE_COUNT.finditer(label)}
    if len(values) == 1:
        return values.pop(), labels
    return None, labels


def _job_links(soup: BeautifulSoup, response_url: str) -> list[str]:
    links = []
    for anchor in soup.select("a[href]"):
        absolute = urljoin(response_url, anchor.get("href", "").strip())
        parts = urlsplit(absolute)
        if parts.scheme == "https" and parts.hostname == LISTING_HOST and JOB_PATH.fullmatch(parts.path):
            if absolute not in links:
                links.append(absolute)
    return links


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    """Parse the company-scoped FreshTeam board's server-rendered listing."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != LISTING_HOST
            or parts.path.rstrip("/") != LISTING_PATH):
        raise ValueError(f"Kaleyra listing came from an unexpected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    headings = [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")]
    text = normalized_body(soup.get_text(" ", strip=True))
    if title != "Careers":
        raise ValueError(f"Kaleyra FreshTeam listing title changed: {title!r}")
    if "Careers" not in headings or "Open Positions" not in headings:
        raise ValueError("Kaleyra FreshTeam listing identity headings changed")
    theme_links = [
        link.get("href", "")
        for link in soup.select("link[href]")
        if "www.kaleyra.com/wp-content/themes/kaleyra" in link.get("href", "")
    ]
    if not theme_links:
        raise ValueError("Kaleyra FreshTeam listing no longer loads the Kaleyra theme assets")
    if not re.search(r"©\s*Kaleyra", text, re.I):
        raise ValueError("Kaleyra FreshTeam listing no longer carries the Kaleyra footer")

    rows, issues, seen = [], [], set()
    for block in soup.select(".job-role-list"):
        title_node = block.select_one(".role-title h5")
        department = normalize_text(title_node.get_text(" ", strip=True)) if title_node else None
        if department:
            department = ROLE_COUNT.sub("", department).strip(" -")
        for card in block.select("ul.job-list > li.heading"):
            anchor = card.select_one('a[href^="/jobs/"]')
            title_tag = card.select_one("h6.job-title")
            preview_tag = card.select_one("p.job-desc")
            location_tags = card.select(".job-location p.paragraph")
            if anchor is None or title_tag is None or len(location_tags) < 2:
                issues.append("kaleyra_listing_card_fields_missing")
                continue
            url = urljoin(response_url, anchor.get("href", "").strip())
            match = JOB_PATH.fullmatch(urlsplit(url).path)
            if match is None:
                issues.append("kaleyra_listing_card_job_path_unexpected")
                continue
            identity, slug = match.groups()
            if identity in seen:
                issues.append(f"kaleyra_listing_duplicate_job_id:{identity}")
                continue
            seen.add(identity)
            rows.append({
                "id": identity,
                "slug": slug,
                "url": url,
                "title": normalize_text(title_tag.get_text(" ", strip=True)),
                "preview": normalize_text(preview_tag.get_text(" ", strip=True)) if preview_tag else "",
                "department": department,
                "employment_label": normalize_text(location_tags[0].get_text(" ", strip=True)),
                "location": normalize_text(location_tags[1].get_text(" ", strip=True)),
                "card_html": str(card),
            })
    all_links = _job_links(soup, response_url)
    row_links = [row["url"] for row in rows]
    if all_links != row_links:
        issues.append("kaleyra_listing_job_links_do_not_match_rendered_cards")
    count, count_labels = _count_open_roles(soup)
    if count is None:
        issues.append("kaleyra_listing_open_role_count_missing_or_ambiguous")
    elif count != len(rows):
        issues.append("kaleyra_listing_open_role_count_does_not_match_rows")
    if not rows and count not in (0, None):
        issues.append("kaleyra_listing_has_count_but_no_rows")
    pagination = [
        normalize_text(node.get_text(" ", strip=True))
        for node in soup.select("a,button")
        if PAGINATION.search(normalize_text(node.get_text(" ", strip=True)))
    ]
    if pagination:
        issues.append("kaleyra_listing_has_unreviewed_pagination_controls")
    filters = []
    for select in soup.select("select"):
        options = [normalize_text(option.get_text(" ", strip=True)) for option in select.select("option")]
        filters.append({"tag": "select", "name": select.get("name"), "options": options})
    for input_ in soup.select("input"):
        if input_.get("type") in (None, "", "text", "search", "checkbox", "submit"):
            filters.append({"tag": "input", "name": input_.get("name"), "type": input_.get("type")})
    return {
        "rows": rows,
        "count": count,
        "count_labels": count_labels,
        "job_ids": [row["id"] for row in rows],
        "job_links": all_links,
        "title": title,
        "headings": headings,
        "departments": sorted({row["department"] for row in rows if row["department"]}),
        "pagination_controls": pagination,
        "filters": filters,
        "branding": {
            "kaleyra_theme_assets": theme_links[:3],
            "kaleyra_footer": True,
        },
        "issues": issues,
    }


def _jsonld_job_posting(soup: BeautifulSoup) -> dict:
    postings = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        types = value.get("@type") if isinstance(value, dict) else None
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            postings.append(value)
    if len(postings) != 1:
        raise ValueError(f"Kaleyra detail expected one JobPosting JSON-LD, found {len(postings)}")
    return postings[0]


def _visible_description(soup: BeautifulSoup) -> str:
    node = soup.select_one(".job-detail")
    if node is None:
        raise ValueError("Kaleyra detail is missing the visible .job-detail description")
    copy = BeautifulSoup(str(node), "lxml").select_one(".job-detail")
    for heading in copy.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if normalize_text(heading.get_text(" ", strip=True)).lower() == "job description":
            heading.decompose()
    return text_html(str(copy))


def _is_heading_block(block: Tag) -> bool:
    if not isinstance(block, Tag):
        return False
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return True
    if block.name != "p":
        return False
    text = normalize_text(block.get_text(" ", strip=True))
    if not text or len(text) > 140:
        return False
    strong = block.find(["strong", "b"], recursive=False)
    if strong is None:
        return False
    return normalize_text(strong.get_text(" ", strip=True)) == text


def description_sections(html: str) -> list[dict]:
    """Keep employer-authored headings and the body below each heading."""
    soup = BeautifulSoup(f'<div data-kaleyra-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-kaleyra-body]")
    if root is None:
        return [{"heading": None, "html": "", "text": ""}]
    sections, current = [], None
    for block in root.contents:
        if not isinstance(block, Tag):
            continue
        if _is_heading_block(block):
            if current is not None:
                sections.append(current)
            current = {"heading": normalize_text(block.get_text(" ", strip=True)), "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        section["text"] = text_html(section["html"]) if section["html"] else ""
    return sections


def parse_detail(html: str, response_url: str, listing_row: dict) -> dict:
    """Parse one FreshTeam detail page and cross-check its JSON-LD body."""
    parts = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.hostname != LISTING_HOST or match is None
            or match.group(1) != str(listing_row.get("id"))):
        raise ValueError(f"Kaleyra detail came from an unexpected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    posting = _jsonld_job_posting(soup)
    hiring = posting.get("hiringOrganization") or {}
    if normalize_text(hiring.get("name") if isinstance(hiring, dict) else None).lower() != EXPECTED_EMPLOYER.lower():
        raise ValueError("Kaleyra detail JSON-LD hiringOrganization is not Kaleyra")
    title = normalize_text(posting.get("title"))
    if title != listing_row["title"]:
        raise ValueError(f"Kaleyra detail title does not match its listed card: {title!r}")
    raw_description = posting.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        raise ValueError("Kaleyra detail JSON-LD has no description")
    description_html = html_module.unescape(raw_description)
    description_text = text_html(description_html)
    visible_text = _visible_description(soup)
    if normalized_body(description_text) != normalized_body(visible_text):
        raise ValueError(VISIBLE_MISMATCH)

    visible_title = next(
        (normalize_text(node.get_text(" ", strip=True)) for node in soup.select(".job-title h1")),
        None,
    )
    if visible_title != title:
        raise ValueError("Kaleyra detail visible title does not match JSON-LD title")
    location_node = soup.select_one(".job-title .stick-hide-in-mobile")
    location_text = normalize_text(location_node.get_text(" ", strip=True)) if location_node else ""
    location, separator, employment = location_text.partition("|")
    location = normalize_text(location) if separator else None
    employment = normalize_text(employment) if separator else None
    department_node = soup.select_one("a.link-back")
    department = normalize_text(department_node.get_text(" ", strip=True)) if department_node else listing_row.get("department")
    apply_anchor = next(
        (anchor for anchor in soup.select("a[href]") if "apply now" in normalize_text(anchor.get_text(" ", strip=True)).lower()),
        None,
    )
    form = soup.select_one("form[action]")
    form_action = urljoin(response_url, form.get("action", "")) if form else None
    if form_action is not None:
        expected_apply = urljoin(response_url, f"/jobs/{listing_row['id']}/applicants")
        if form_action != expected_apply:
            raise ValueError("Kaleyra detail application form action changed")
    address = ((posting.get("jobLocation") or {}).get("address") or {})
    remote = posting.get("remote")
    sections = description_sections(description_html)
    responsibilities = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower() for token in HIRING_HEADINGS)
    ]
    qualifications = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower() for token in QUALIFICATION_HEADINGS)
    ]
    return {
        "title": title,
        "posting": posting,
        "description_html": description_html,
        "description_text": description_text,
        "visible_description_text": visible_text,
        "description_sections": sections,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "location": location,
        "employment_label": employment,
        "department": department,
        "apply_anchor": {
            "text": normalize_text(apply_anchor.get_text(" ", strip=True)) if apply_anchor else None,
            "href": apply_anchor.get("href") if apply_anchor else None,
        },
        "form_action": form_action,
        "job_location": posting.get("jobLocation"),
        "address": address if isinstance(address, dict) else {},
        "remote": remote,
        "date_posted_raw": posting.get("datePosted"),
        "employment_type_raw": posting.get("employmentType"),
        "hiring_organization": hiring,
        "description_sha256": sha256(description_text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(description_text),
        "visible_description_sha256": sha256(visible_text.encode()).hexdigest(),
        "visible_description_fnv1a64": fnv1a64(visible_text),
        "description_chars": len(description_text),
        "visible_description_chars": len(visible_text),
        "description_matches_visible": True,
    }


def _requisition_id(title: str) -> str | None:
    match = REQUISITION.search(title or "")
    return match.group(1) if match else None


def _detail_headers(response, response_url: str) -> dict:
    return {
        "url": response_url,
        "status": response.status_code,
        "final_url": str(response.url),
        "response_sha256": sha256(response.content).hexdigest(),
        "response_bytes": len(response.content),
    }


class Kaleyra(Adapter):
    """Collect only Kaleyra's company-scoped FreshTeam board."""

    VERIFICATION_REVISION = "kaleyra.official-careers-subdomain-freshteam.1"

    def __init__(self, http, company, source, options, *, resolver=None):
        super().__init__(http, company, source, options)
        self.resolver = resolver or socket.gethostbyname_ex
        self.failed_jobs: list[dict] = []

    def _changed(self, error=CHANGED):
        self.result.errors.append(error)
        return self.result

    async def _official_entry_probe(self):
        started = time.perf_counter()
        try:
            response = await self.http.get(OFFICIAL_ENTRY)
        except FetchError as exc:
            self.result.errors.append(f"kaleyra_official_entry_fetch_failed: {exc}")
            self.result.timings["entry_probe_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            return None
        seconds = max(0.0, time.perf_counter() - started)
        final_url = str(response.url)
        soup = BeautifulSoup(response.text, "lxml")
        entry = {
            "requested_url": OFFICIAL_ENTRY,
            "final_url": final_url,
            "status": response.status_code,
            "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None,
            "redirects": [
                {"status": hop.status_code, "url": str(hop.url), "location": hop.headers.get("location")}
                for hop in response.history
            ],
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": round(seconds, 6),
        }
        self.result.timings["entry_probe_seconds"] = round(seconds, 6)
        self.result.metadata["official_entry"] = entry
        if final_url != PARENT_PORTAL:
            self.result.errors.append(CHANGED)
            return None
        return entry

    def _resolve_careers_subdomain(self):
        started = time.perf_counter()
        try:
            canonical, aliases, addresses = self.resolver(CAREERS_SUBDOMAIN_HOST)
        except Exception as exc:
            self.result.errors.append(f"{CNAME_FAILED}: {type(exc).__name__}: {exc}")
            self.result.metadata["careers_subdomain"] = {
                "host": CAREERS_SUBDOMAIN_HOST,
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.result.timings["dns_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            return None
        seconds = max(0.0, time.perf_counter() - started)
        record = {
            "host": CAREERS_SUBDOMAIN_HOST,
            "canonical_name": canonical,
            "aliases": list(aliases),
            "addresses": list(addresses),
            "matches_listing_host": canonical == LISTING_HOST,
            "seconds": round(seconds, 6),
        }
        self.result.timings["dns_seconds"] = round(seconds, 6)
        self.result.metadata["careers_subdomain"] = record
        if canonical != LISTING_HOST:
            self.result.errors.append(CNAME_CHANGED)
            return None
        return record

    async def _custom_host_probe(self):
        started = time.perf_counter()
        try:
            response = await self.http.get(CUSTOM_HOST_URL)
        except FetchError as exc:
            seconds = max(0.0, time.perf_counter() - started)
            self.result.timings["custom_host_probe_seconds"] = round(seconds, 6)
            self.result.metadata["custom_host"] = {
                "requested_url": CUSTOM_HOST_URL,
                "reachable": False,
                "error": str(exc),
                "seconds": round(seconds, 6),
            }
            return None
        seconds = max(0.0, time.perf_counter() - started)
        record = {
            "requested_url": CUSTOM_HOST_URL,
            "final_url": str(response.url),
            "status": response.status_code,
            "reachable": True,
            "redirects": [
                {"status": hop.status_code, "url": str(hop.url), "location": hop.headers.get("location")}
                for hop in response.history
            ],
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": round(seconds, 6),
        }
        self.result.timings["custom_host_probe_seconds"] = round(seconds, 6)
        self.result.metadata["custom_host"] = record
        return record

    async def _listing_probe(self, phase: str):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_URL:
            raise ValueError(f"Kaleyra listing redirected away from its reviewed FreshTeam route: {response.url}")
        parsed = parse_listing(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            final_url=str(response.url),
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            seconds=round(seconds, 6),
            phase=phase,
        )
        self.result.timings[f"listing_{phase}_seconds"] = round(seconds, 6)
        return parsed

    async def _detail_probe(self, row):
        started = time.perf_counter()
        response = await self.http.get(row["url"])
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != row["url"]:
            raise ValueError(f"Kaleyra detail redirected away from its listed route: {row['url']}")
        parsed = parse_detail(response.text, str(response.url), row)
        parsed["response"] = _detail_headers(response, row["url"])
        parsed["seconds"] = round(seconds, 6)
        return parsed

    def _make_job(self, row, detail):
        title = detail["title"]
        date_posted = parse_date_posted(detail["date_posted_raw"])
        requisition = _requisition_id(title)
        workplace = "Remote" if str(detail.get("remote")).lower() == "true" else None
        location = detail.get("location") or row.get("location")
        raw = {
            "listing_card": row,
            "detail": {
                "jsonld": detail["posting"],
                "hiring_organization": detail["hiring_organization"],
                "job_location": detail["job_location"],
                "date_posted_raw": detail["date_posted_raw"],
                "employment_type_raw": detail["employment_type_raw"],
                "remote_raw": detail.get("remote"),
                "visible_location": detail.get("location"),
                "visible_employment_label": detail.get("employment_label"),
                "department": detail.get("department"),
                "apply_anchor": detail.get("apply_anchor"),
                "form_action": detail.get("form_action"),
                "description_sections": detail["description_sections"],
                "description_sha256": detail["description_sha256"],
                "description_fnv1a64": detail["description_fnv1a64"],
                "visible_description_sha256": detail["visible_description_sha256"],
                "visible_description_fnv1a64": detail["visible_description_fnv1a64"],
                "description_chars": detail["description_chars"],
                "visible_description_chars": detail["visible_description_chars"],
                "description_matches_visible": detail["description_matches_visible"],
                "response": detail["response"],
            },
        }
        apply_href = (detail.get("apply_anchor") or {}).get("href")
        apply_url = urljoin(row["url"], apply_href) if apply_href else row["url"]
        job = self.job(
            raw,
            title=title,
            url=row["url"],
            job_id=row["id"],
            requisition_id=requisition,
            apply_url=apply_url,
            description=detail["description_html"],
            method="freshteam.job_posting_jsonld.description_verified_against_visible_job_detail",
            description_url=row["url"],
            locations=[location] if location else [],
            department=detail.get("department") or row.get("department"),
            employment_type=detail.get("employment_label"),
            workplace_type=workplace,
            date_posted=date_posted,
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=detail.get("qualifications"),
            responsibilities=detail.get("responsibilities"),
            experience=None,
            education=None,
            employer=EXPECTED_EMPLOYER,
            language="en",
        )
        job.description_text = detail["description_text"]
        if date_posted is None:
            job.warnings.append("source_date_unparsed")
        if workplace is None and "(remote)" in title.lower():
            job.warnings.append("title_remote_suffix_conflicts_with_structured_remote_false")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "parent_portal_url": PARENT_PORTAL,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "Kaleyra's own careers.kaleyra.com subdomain CNAMEs to kaleyra-talent.freshteam.com; "
                "the FreshTeam board is Kaleyra-branded and every role's JSON-LD hiringOrganization is Kaleyra."
            ),
            "scope_decision": (
                "The ledger URL 301-redirects to the Tata Communications parent board (244 roles, no Kaleyra "
                "company/brand facet), which is rejected. The company-scoped Kaleyra FreshTeam board on the "
                "official careers subdomain is collected. The custom hostname is broken (TLS/409), so the same "
                "board's native FreshTeam host is used."
            ),
            "parent_board_scope_rejected": True,
            "parent_board_api_requests": 0,
            "detail_hydration_mode": (
                "Server-rendered FreshTeam detail pages; JSON-LD JobPosting description is cross-checked "
                "against the visible .job-detail block before a job is accepted."
            ),
            "request_scope": (
                "Official entry redirect, documented custom host probe, official careers subdomain DNS, "
                "Kaleyra FreshTeam listing and its 3 exact detail pages. No parent-board API, guessed ATS "
                "slug, aggregator, or other company board is queried."
            ),
            "failed_jobs": [],
        })
        self.result.expected_total = None
        self.result.listing_complete = False
        try:
            if not self.page_guard():
                return self.result
            entry = await self._official_entry_probe()
            if entry is None:
                return self.result
            if not self.page_guard():
                return self.result
            cname = self._resolve_careers_subdomain()
            if cname is None:
                return self.result
            if not self.page_guard():
                return self.result
            await self._custom_host_probe()
            if not self.page_guard():
                return self.result
            initial = await self._listing_probe("initial")
            self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
            self.result.metadata["initial_job_ids"] = initial["job_ids"]
            if initial["issues"]:
                self.result.errors.extend(initial["issues"])
                return self.result
            self.result.expected_total = initial["count"]
            if initial["count"] is None or initial["count"] != len(initial["rows"]):
                self.result.errors.append("kaleyra_initial_listing_count_does_not_match_rows")
                return self.result
            if not initial["rows"]:
                # A verified accessible empty company board is a real zero-job listing.
                self.result.listing_complete = True
                return self.result

            jobs, detail_seconds = [], 0.0
            for row in initial["rows"]:
                if not self.page_guard():
                    return self.result
                if self.limit():
                    return self.result
                try:
                    detail = await self._detail_probe(row)
                    detail_seconds += detail["seconds"]
                    job = self._make_job(row, detail)
                    self.add(job)
                    jobs.append(job)
                except Exception as exc:
                    message = f"Kaleyra detail {row['id']} ({row['url']}): {type(exc).__name__}: {exc}"
                    self.result.errors.append(message)
                    self.failed_jobs.append({"id": row["id"], "url": row["url"], "reason": str(exc)})
                    self.result.metadata["failed_jobs"] = list(self.failed_jobs)
                    # Preserve a usable listing record but mark its description unavailable.
                    job = self.job(
                        {"listing_card": row, "detail_error": message},
                        title=row["title"], url=row["url"], job_id=row["id"],
                        requisition_id=_requisition_id(row["title"]), apply_url=row["url"],
                        description="", method=None, description_url=row["url"],
                        locations=[row["location"]] if row.get("location") else [],
                        department=row.get("department"),
                        employment_type=row.get("employment_label"),
                        employer=EXPECTED_EMPLOYER, language="en",
                    )
                    job.description_unavailable = True
                    job.warnings.append("detail_fetch_failed")
                    self.add(job)
                    jobs.append(job)
            self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)

            if not self.page_guard():
                return self.result
            final = await self._listing_probe("final")
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["final_job_ids"] = final["job_ids"]
            final_matches = (
                final["job_ids"] == initial["job_ids"]
                and final["count"] == initial["count"]
                and final["job_links"] == initial["job_links"]
                and not final["issues"]
            )
            changed_fields = {}
            initial_by_id = {row["id"]: row for row in initial["rows"]}
            final_by_id = {row["id"]: row for row in final["rows"]}
            for identity in initial["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            if changed_fields:
                final_matches = False
            final_details = {}
            final_detail_seconds = 0.0
            for row in initial["rows"]:
                if not self.page_guard():
                    return self.result
                try:
                    detail = await self._detail_probe(row)
                    final_detail_seconds += detail["seconds"]
                    final_details[row["id"]] = {
                        "description_sha256": detail["description_sha256"],
                        "date_posted_raw": detail["date_posted_raw"],
                        "title": detail["title"],
                    }
                except Exception as exc:
                    message = f"Kaleyra final detail {row['id']} ({row['url']}): {type(exc).__name__}: {exc}"
                    self.result.errors.append(message)
                    final_matches = False
            self.result.timings["final_detail_recheck_seconds"] = round(final_detail_seconds, 6)
            initial_details = {
                job.job_id: {
                    "description_sha256": job.raw.get("detail", {}).get("description_sha256"),
                    "date_posted_raw": job.raw.get("detail", {}).get("date_posted_raw"),
                    "title": job.title,
                }
                for job in jobs if job.raw.get("detail")
            }
            details_changed = {
                identity: {key: (initial_details[identity].get(key), final_details.get(identity, {}).get(key))
                          for key in ("description_sha256", "date_posted_raw", "title")
                          if initial_details[identity].get(key) != final_details.get(identity, {}).get(key)}
                for identity in initial_details
                if initial_details[identity] != final_details.get(identity)
            }
            if details_changed:
                final_matches = False
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_pages": 1,
                "final_pages": 1,
                "initial_final_ids_and_order_match": initial["job_ids"] == final["job_ids"],
                "initial_final_card_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "details_changed_by_job_id": details_changed,
                "new_job_ids": [i for i in final["job_ids"] if i not in initial["job_ids"]],
                "removed_job_ids": [i for i in initial["job_ids"] if i not in final["job_ids"]],
                "pagination": "none: the FreshTeam listing has no pagination controls; page 2 returns no job links.",
                "filters": "location select (Bengaluru), remote checkbox and query box; no company/brand filter.",
                "complete": bool(final_matches and len(jobs) == initial["count"] and all(job.description_verified for job in jobs)),
            }
            if not final_matches:
                self.result.errors.append("kaleyra_listing_membership_changed_during_collection")
            if len(jobs) != initial["count"]:
                self.result.errors.append("kaleyra_retained_rows_do_not_match_company_listing_count")
            self.result.jobs = jobs
            self.result.metadata["job_ids"] = [job.job_id for job in jobs]
            self.result.metadata["listing_count"] = initial["count"]
            self.result.metadata["listing_role_count_labels"] = initial["count_labels"]
            self.result.metadata["listing_departments"] = initial["departments"]
            self.result.metadata["listing_filters"] = initial["filters"]
            self.result.metadata["listing_branding"] = initial["branding"]
            self.result.metadata["descriptions_verified"] = sum(job.description_verified for job in jobs)
            self.result.metadata["failed_jobs"] = list(self.failed_jobs)
            self.result.listing_complete = bool(
                final_matches
                and len(jobs) == initial["count"]
                and len(jobs) == len(initial["rows"])
                and all(job.description_verified for job in jobs)
                and not self.result.errors
            )
            return self.result
        finally:
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6
            )
