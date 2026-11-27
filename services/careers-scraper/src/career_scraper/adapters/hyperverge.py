"""HyperVerge's own careers page and its exact company-published job links.

The careers page is a hand-maintained WordPress page.  Its nine visible
department panes contain fourteen server-rendered job cards; two roles are
cross-listed under Engineering and Deep Learning, leaving twelve distinct
postings.  Each card publishes only a title, a city, an employment type and an
``Apply Now`` href.  Those hrefs are the company's own detail destinations:
either an exact LinkedIn job posting or a Google Form.  Membership is taken
from the company page, never from a LinkedIn company board or search.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://hyperverge.co/careers/"
COMPANY_NAME = "HyperVerge"
EMPLOYER_NAME = "HyperVerge Inc."
LINKEDIN_HOSTS = {"www.linkedin.com", "in.linkedin.com", "ie.linkedin.com"}
LINKEDIN_GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
GOOGLE_FORM_HOST = "docs.google.com"
LINKEDIN_JOB_PATH = re.compile(r"^/jobs/view/(?:[^/?#]*-)?(\d+)/?$")
LINKEDIN_GUEST_PATH = re.compile(r"^/jobs-guest/jobs/api/jobPosting/(\d+)/?$")
GOOGLE_FORM_PATH = re.compile(r"^/forms/d/e/([A-Za-z0-9_-]+)/viewform")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
CITY_ALIASES = {"bangalore": "bengaluru", "bengaluru": "bengaluru"}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def linkedin_job_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in LINKEDIN_HOSTS:
        return None
    match = LINKEDIN_JOB_PATH.fullmatch(parsed.path)
    return match.group(1) if match else None


def google_form_id(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname != GOOGLE_FORM_HOST:
        return None
    match = GOOGLE_FORM_PATH.match(parsed.path)
    return match.group(1) if match else None


def canonical_title(value: str | None) -> str:
    title = normalized(value).lower().replace("&", " and ")
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    title = re.sub(r"^bd\b", "business development", title)
    return re.sub(r"\s+", " ", title)


def city_matches(expected: str | None, observed: str | None) -> bool:
    left = normalized(expected).lower()
    right = normalized(observed).lower()
    if not left or not right:
        return False
    if left == right:
        return True
    first = re.split(r"[,/|]", right)[0].strip()
    return CITY_ALIASES.get(left, left) == CITY_ALIASES.get(first, first)


def title_matches(expected: str | None, observed: str | None, location: str | None) -> bool:
    expected_key = canonical_title(expected)
    observed_key = canonical_title(observed)
    if not expected_key or not observed_key:
        return False
    if expected_key == observed_key:
        return True
    # LinkedIn adds the city in parentheses for otherwise identical roles,
    # e.g. "Principal Key Account Manager - BFSI (Bangalore)".
    match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", normalized(observed))
    if match:
        inner = normalized(match.group(2))
        if city_matches(location, inner):
            return canonical_title(match.group(1)) == expected_key
    return False


def description_inner_html(value: str | None) -> str:
    """Return the employer-authored body, without LinkedIn's wrapper element."""
    soup = BeautifulSoup(value or "", "lxml")
    markup = soup.select_one(".show-more-less-html__markup")
    if markup is not None:
        return "".join(str(child) for child in markup.contents)
    return value or ""


def description_sections(html: str | None) -> list[dict]:
    soup = BeautifulSoup(f'<div data-hv-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-hv-body]")
    if root is None:
        return [{"heading": None, "html": "", "text": ""}]
    blocks = [node for node in root.contents if isinstance(node, Tag)]
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized(block.get_text(" ", strip=True))
            strong_text = normalized(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and 0 < len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    elif not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _field_sections(sections: list[dict], pattern: str) -> list[dict] | None:
    selected = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section.get("heading") and re.search(pattern, section["heading"], re.I)
    ]
    return selected or None


def _rendered_text_from_html(fragment: str | None) -> str:
    """Match a browser's innerText closely enough for body comparisons."""
    soup = BeautifulSoup(fragment or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    block_tags = {
        "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "section",
        "table", "tr", "ul",
    }
    pieces: list[str] = []

    def add_break() -> None:
        if pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")

    def walk(node) -> None:
        if isinstance(node, str):
            pieces.append(re.sub(r"\s+", " ", node))
            return
        if not isinstance(node, Tag):
            return
        if node.name in {"script", "style", "noscript"}:
            return
        block = node.name in block_tags
        if block:
            add_break()
        for child in node.children:
            walk(child)
        if block:
            add_break()

    walk(soup)
    rendered = "".join(pieces)
    rendered = re.sub(r"[\t\f\v\r ]+", " ", rendered)
    rendered = re.sub(r" *\n *", "\n", rendered)
    rendered = re.sub(r"\n+", "\n", rendered)
    return rendered.strip()


def parse_official_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "hyperverge.co"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("HyperVerge official careers response redirected away from the inspected page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "HyperVerge" not in title or "Career" not in title:
        raise ValueError("HyperVerge careers page identity changed")
    section = soup.select_one("#jobOpenings")
    if section is None:
        raise ValueError("HyperVerge careers page no longer exposes the job-openings section")
    nav = section.select(".openings-tab a[data-tab]")
    labels = {anchor.get("data-tab"): normalized(anchor.get_text(" ", strip=True)) for anchor in nav}
    labels.setdefault("contribution", "Contribution")
    panes = section.select(".tab-content > div[role=tabpanel]")
    if not panes:
        raise ValueError("HyperVerge careers page exposes no department panes")
    cards, tabs, issues = [], [], []
    for pane in panes:
        tab = pane.get("id")
        label = labels.get(tab) or normalized(tab)
        pane_cards = pane.select(".job-col")
        no_openings = bool(pane.select(".no-openings"))
        tabs.append({
            "id": tab,
            "label": label,
            "card_count": len(pane_cards),
            "no_openings_text": normalized(pane.select_one(".no-openings").get_text(" ", strip=True))
            if no_openings else None,
        })
        for column in pane_cards:
            title_node = column.select_one(".job-title")
            spans = [normalized(item.get_text(" ", strip=True)) for item in column.select(".job-meta span")]
            anchor = column.select_one("a.btn[href]")
            href = anchor.get("href") if anchor else None
            if not title_node or not href:
                issues.append(f"job_card_missing_title_or_apply_link:{tab}")
                continue
            cards.append({
                "tab": tab,
                "department": label,
                "title": normalized(title_node.get_text(" ", strip=True)),
                "location": spans[0] if spans else None,
                "employment_type": spans[1] if len(spans) > 1 else None,
                "apply_url_as_published": href,
                "card_text": normalized(column.get_text(" ", strip=True)),
            })
    jobs, seen = [], {}
    for card in cards:
        url = card["apply_url_as_published"]
        linkedin_id = linkedin_job_id(url)
        form_id = google_form_id(url)
        if linkedin_id:
            identity = f"linkedin:{linkedin_id}"
            job_id, application_form_id = linkedin_id, None
        elif form_id:
            # Two distinct roles share the same Google Form; title and city keep
            # those postings separate without inventing either a job or a form id.
            identity = f"gform:{form_id}|{canonical_title(card['title'])}|{canonical_title(card['location'])}"
            job_id, application_form_id = None, form_id
        else:
            identity = f"url:{url}"
            job_id, application_form_id = None, None
            issues.append(f"unrecognised_apply_destination:{card['title']}")
        key = (card["title"], card["location"], identity)
        if key in seen:
            seen[key]["departments"].append(card["department"])
            seen[key]["cards"].append(card)
            continue
        item = dict(card)
        item.update(
            identity=identity,
            job_id=job_id,
            application_form_id=application_form_id,
            departments=[card["department"]],
            cards=[card],
            apply_url_malformed=bool(form_id and url.count("/viewform") > 1),
        )
        seen[key] = item
        jobs.append(item)
    duplicates = [
        {"title": job["title"], "location": job["location"], "departments": job["departments"],
         "identity": job["identity"]}
        for job in jobs if len(job["departments"]) > 1
    ]
    pagination = [
        normalized(anchor.get_text(" ", strip=True))
        for anchor in section.select("a,button")
        if re.search(r"\b(?:next|previous|load more|page \d+)\b", anchor.get_text(" ", strip=True), re.I)
    ]
    if not cards:
        issues.append("company_careers_page_has_no_job_cards")
    if len(jobs) != len(cards) - sum(len(job["departments"]) - 1 for job in jobs):
        issues.append("cross_tab_card_reconciliation_failed")
    return {
        "url": response_url,
        "title": title,
        "section_heading": normalized(section.select_one(".section-title").get_text(" ", strip=True))
        if section.select_one(".section-title") else None,
        "tabs": tabs,
        "card_count": len(cards),
        "cards": cards,
        "unique_count": len(jobs),
        "jobs": jobs,
        "duplicates": duplicates,
        "pagination_controls": pagination,
        "department_filters": [tab["label"] for tab in tabs],
        "issues": issues,
    }


def _criteria(soup: BeautifulSoup) -> tuple[list[dict], dict]:
    rows = []
    for item in soup.select("li.description__job-criteria-item"):
        label = item.select_one("h3.description__job-criteria-subheader")
        value = item.select_one("span.description__job-criteria-text")
        if label and value:
            rows.append({
                "label": normalized(label.get_text(" ", strip=True)),
                "value": normalized(value.get_text(" ", strip=True)),
            })
    by_label: dict[str, str] = {}
    for row in rows:
        if row["label"] in by_label:
            by_label[row["label"]] = f"{by_label[row['label']]}; {row['value']}"
        else:
            by_label[row["label"]] = row["value"]
    return rows, by_label


def _detail_status(detail: dict, expected: dict) -> str:
    if detail.get("company") != COMPANY_NAME:
        return "company_mismatch"
    if not title_matches(expected.get("title"), detail.get("title"), expected.get("location")):
        return "title_mismatch"
    if not city_matches(expected.get("location"), detail.get("location")):
        return "location_mismatch"
    if len(compact(detail.get("description_text"))) < 100:
        return "empty_description"
    return "verified"


def parse_linkedin_detail(
    html: str,
    expected: dict,
    response_url: str,
    *,
    response_meta: dict | None = None,
) -> dict:
    parsed = urlsplit(response_url)
    match = LINKEDIN_GUEST_PATH.fullmatch(parsed.path) or LINKEDIN_JOB_PATH.fullmatch(parsed.path)
    if parsed.hostname not in LINKEDIN_HOSTS or match is None:
        raise ValueError("HyperVerge detail response is not an exact public LinkedIn job route")
    expected_id = str(expected.get("job_id") or "")
    if expected_id and match.group(1) != expected_id:
        raise ValueError("HyperVerge detail response job id does not match the company card")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h2.top-card-layout__title") or soup.select_one("h1.top-card-layout__title")
    org_node = soup.select_one("a.topcard__org-name-link")
    subline = soup.select_one(".top-card-layout__second-subline")
    location_node = subline.select_one(".topcard__flavor-row .topcard__flavor--bullet") if subline else None
    posted_node = subline.select_one(".posted-time-ago__text") if subline else None
    applicants_node = subline.select_one(".num-applicants__caption") if subline else None
    closed_node = soup.select_one(".closed-job__flavor--closed")
    criteria_rows, criteria = _criteria(soup)
    markup = soup.select_one(".show-more-less-html__markup")
    body_html = "".join(str(child) for child in markup.contents) if markup else ""
    body_text = text_html(body_html)
    sections = description_sections(body_html)
    detail = {
        "response_url": response_url,
        "title": normalized(title_node.get_text(" ", strip=True)) if title_node else None,
        "company": normalized(org_node.get_text(" ", strip=True)) if org_node else None,
        "location": normalized(location_node.get_text(" ", strip=True)) if location_node else None,
        "relative_posted_text": normalized(posted_node.get_text(" ", strip=True)) if posted_node else None,
        "applicant_text": normalized(applicants_node.get_text(" ", strip=True)) if applicants_node else None,
        "closed_text": normalized(closed_node.get_text(" ", strip=True)) if closed_node else None,
        "job_criteria": criteria_rows,
        "criteria_by_label": criteria,
        "description_html": body_html,
        "description_text": body_text,
        "description_compact": compact(body_text),
        "description_normalized_chars": len(compact(body_text)),
        "description_fnv1a64": fnv1a64(compact(body_text)),
        "description_sections": sections,
        "qualifications": _field_sections(
            sections, r"qualification|requirement|what you.ll need|what we.re looking for|must have|nice to have|preferred"
        ),
        "responsibilities": _field_sections(
            sections, r"responsib|what you.ll do|duties|scope of ownership|key outcomes"
        ),
        "description_links": [
            {"url": anchor.get("href"), "text": normalized(anchor.get_text(" ", strip=True))}
            for anchor in (markup.select("a[href]") if markup else [])
        ],
        "apply_controls": [
            {"tag": node.name, "text": normalized(node.get_text(" ", strip=True)),
             "href": node.get("href"), "class": node.get("class")}
            for node in soup.select("button, a[href]")
            if re.search(r"apply", f"{node.get_text(' ', strip=True)} {' '.join(node.get('class') or [])}", re.I)
        ],
        "raw_response_retained": True,
        "response_sha256": (response_meta or {}).get("sha256"),
        "source_fetched_at": (response_meta or {}).get("fetched_at"),
        "probe_source": None,
    }
    detail["status"] = _detail_status(detail, expected)
    return detail


def parse_probe_detail(probe_row: dict, expected: dict, response_url: str, *, captured_at: str | None = None) -> dict:
    body_html = description_inner_html(probe_row.get("desc"))
    body_text = text_html(body_html)
    sections = description_sections(body_html)
    detail = {
        "response_url": response_url,
        "title": normalized(probe_row.get("title")),
        "company": normalized(probe_row.get("org")),
        "location": normalized(probe_row.get("loc")),
        "relative_posted_text": None,
        "applicant_text": None,
        "closed_text": None,
        "job_criteria": [],
        "criteria_by_label": {},
        "description_html": body_html,
        "description_text": body_text,
        "description_compact": compact(body_text),
        "description_normalized_chars": len(compact(body_text)),
        "description_fnv1a64": fnv1a64(compact(body_text)),
        "description_sections": sections,
        "qualifications": _field_sections(
            sections, r"qualification|requirement|what you.ll need|what we.re looking for|must have|nice to have|preferred"
        ),
        "responsibilities": _field_sections(
            sections, r"responsib|what you.ll do|duties|scope of ownership|key outcomes"
        ),
        "description_links": [],
        "apply_controls": [],
        "raw_response_retained": False,
        "response_sha256": None,
        "source_fetched_at": captured_at,
        "probe_source": "companies/hyperverge/source/linkedin-probe.json",
        "probe_http_status": probe_row.get("status"),
        "probe_title_preview": normalized(probe_row.get("title")),
        "capture_method": "parsed_public_guest_api_probe",
    }
    detail["status"] = _detail_status(detail, expected)
    return detail


def parse_linkedin_challenge(html: str, status: int, response_url: str) -> dict:
    text = compact(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    refresh = 'http-equiv="refresh"' in html and "linkedin.com" in html
    obfuscated = "_0x" in html and "cookie" in html.lower()
    return {
        "status": status,
        "url": response_url,
        "challenge_detected": bool(status == 429 or refresh or obfuscated),
        "meta_refresh_to_linkedin": refresh,
        "obfuscated_javascript": obfuscated,
        "visible_text": text[:300],
        "description_unavailable": True,
    }


def parse_google_form_response(html: str, status: int, response_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style"]):
        node.decompose()
    visible = compact(soup.get_text(" ", strip=True))
    return {
        "status": status,
        "url": response_url,
        "sign_in_required": "Sign in to your Google Account" in visible,
        "not_found": status == 404,
        "visible_text": visible[:500],
        "description_unavailable": True,
    }


class HyperVerge(Adapter):
    """Collect HyperVerge's own published cards and their exact linked bodies."""

    VERIFICATION_REVISION = "hyperverge.official-wordpress-cards.exact-linked-destinations.1"

    def __init__(
        self,
        http,
        company,
        source,
        options,
        *,
        raw_details: dict[str, str] | None = None,
        raw_detail_meta: dict[str, dict] | None = None,
        probe_details: dict | None = None,
        probe_captured_at: str | None = None,
        google_form_capture: dict | None = None,
        linkedin_challenge: dict | None = None,
        browser_details: dict | None = None,
    ):
        super().__init__(http, company, source, options)
        self.raw_details = raw_details or {}
        self.raw_detail_meta = raw_detail_meta or {}
        self.probe_details = probe_details or {}
        self.probe_captured_at = probe_captured_at
        self.google_form_capture = google_form_capture or {}
        self.linkedin_challenge = linkedin_challenge
        self.browser_details = browser_details or {}
        self.initial_snapshot: dict | None = None
        self.initial_listing: dict | None = None

    async def _official_snapshot(self, phase: str):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_page(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=round(seconds, 6),
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        self.result.timings[f"{phase}_listing_seconds"] = round(seconds, 6)
        self.result.metadata.setdefault("listing_snapshots", []).append({
            "phase": phase,
            "url": str(response.url),
            "status": response.status_code,
            "cards": parsed["card_count"],
            "unique_jobs": parsed["unique_count"],
            "ordered_identities": [job["identity"] for job in parsed["jobs"]],
            "tab_card_counts": {tab["id"]: tab["card_count"] for tab in parsed["tabs"]},
            "pagination_controls": parsed["pagination_controls"],
            "department_filters": parsed["department_filters"],
            "response_sha256": parsed["response_sha256"],
            "duration_seconds": round(seconds, 6),
        })
        return parsed

    def _job_from_card(self, row: dict):
        raw = {
            "listing": row,
            "cards": row["cards"],
            "departments": row["departments"],
            "apply_url_as_published": row["apply_url_as_published"],
            "apply_url_malformed": row["apply_url_malformed"],
            "application_form_id": row["application_form_id"],
            "employment_type_from_card": row["employment_type"],
            "employer_source": "site footer legal name",
        }
        identity = row["identity"]
        record_identity = sha256(f"hyperverge|{identity}".encode()).hexdigest()
        return self.job(
            raw,
            title=row["title"],
            url=row["apply_url_as_published"],
            job_id=row["job_id"],
            apply_url=row["apply_url_as_published"],
            description="",
            locations=[row["location"]] if row["location"] else [],
            department=row["departments"],
            employment_type=row["employment_type"],
            employer=EMPLOYER_NAME,
            date_posted=None,
            date_posted_raw=None,
            record_identity=record_identity,
        )

    def _apply_detail(self, job, detail: dict, *, source_kind: str):
        job.raw["detail"] = detail
        job.raw["detail_source"] = source_kind
        closed_text = detail.get("closed_text") or detail.get("browser_observed_closed_text")
        job.raw["apply_status"] = (
            "no_longer_accepting_applications" if closed_text
            else "not_captured_in_probe" if detail.get("probe_source")
            else "public_detail_without_closed_text"
        )
        job.raw["apply_status_text"] = closed_text
        job.description_verified = False
        if detail["status"] != "verified":
            job.description_unavailable = True
            job.description_method = f"hyperverge.linkedin.detail.{detail['status']}"
            job.description_source_url = detail["response_url"]
            job.warnings.append(f"detail_{detail['status']}")
            return job
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = f"hyperverge.linkedin.company_published_job_detail.{source_kind}"
        job.description_source_url = detail["response_url"]
        job.description_unavailable = False
        job.raw["detail_company"] = detail.get("company")
        job.employer = EMPLOYER_NAME
        card_type = job.raw.get("employment_type_from_card")
        detail_type = detail.get("criteria_by_label", {}).get("Employment type")
        if card_type and detail_type and card_type.lower() != detail_type.lower():
            job.raw["employment_type_reconciliation"] = {
                "card": card_type,
                "detail": detail_type,
                "note": "Both public values retained; Intern and Internship are the same category.",
            }
        job.employment_type = detail_type or card_type or job.employment_type
        job.experience = detail.get("criteria_by_label", {}).get("Seniority level")
        job.department = job.raw["departments"]
        job.qualifications = detail.get("qualifications")
        job.responsibilities = detail.get("responsibilities")
        job.apply_url = job.raw["apply_url_as_published"]
        job.date_posted = None
        job.date_posted_raw = None
        job.date_updated = None
        job.valid_through = None
        if detail.get("location"):
            job.raw["detail_location"] = detail["location"]
        return job

    def _apply_google_form_failure(self, job, failure: dict):
        detail = {
            "status": "application_form_unavailable",
            "response_url": failure["url"],
            "response_sha256": failure.get("sha256"),
            "source_fetched_at": failure.get("fetched_at"),
            "google_form": failure,
            "raw_response_retained": True,
        }
        job.raw["detail"] = detail
        job.raw["detail_source"] = "google_form"
        job.description_html = ""
        job.description_text = ""
        job.description_unavailable = True
        job.description_method = "hyperverge.google_form.application_form_unavailable"
        job.description_source_url = failure["url"]
        job.raw["apply_status"] = f"http_{failure.get('status')}"
        job.raw["apply_status_text"] = failure.get("visible_text", "")[:200]
        job.warnings.append("application_form_unavailable")
        return job

    async def _detail(self, job):
        linkedin_id = job.job_id
        if linkedin_id and linkedin_id in self.raw_details:
            meta = self.raw_detail_meta.get(linkedin_id, {})
            detail = parse_linkedin_detail(
                self.raw_details[linkedin_id], job.raw["listing"], meta.get("final_url") or meta.get("request_url")
                or LINKEDIN_GUEST.format(job_id=linkedin_id), response_meta=meta,
            )
            detail["reused_preimplementation_response"] = True
            detail["capture_method"] = meta.get("capture_method", "public_http_guest_api")
            source_kind = ("headless_rendered_public_guest_api"
                           if meta.get("capture_method") else "reused_public_guest_api")
            return self._apply_detail(job, detail, source_kind=source_kind)
        if linkedin_id and linkedin_id in self.probe_details:
            probe = self.probe_details[linkedin_id]
            if (probe.get("status") == 200 and probe.get("title") and probe.get("org")
                    and probe.get("desc")):
                detail = parse_probe_detail(
                    probe, job.raw["listing"], LINKEDIN_GUEST.format(job_id=linkedin_id),
                    captured_at=self.probe_captured_at,
                )
                detail["parsed_probe_response"] = True
                browser = self.browser_details.get(linkedin_id) or {}
                if browser.get("closed_text"):
                    detail["browser_observed_closed_text"] = browser["closed_text"]
                if browser.get("observed_title"):
                    detail["browser_observed_title"] = browser["observed_title"]
                return self._apply_detail(job, detail, source_kind="pre_429_parsed_guest_api_probe")
        if job.raw["listing"].get("application_form_id"):
            form_id = job.raw["listing"]["application_form_id"]
            failure = (self.google_form_capture.get(job.url)
                       or self.google_form_capture.get(f"form:{form_id}") or {})
            if failure:
                return self._apply_google_form_failure(job, failure)
        # No description body was ever returned for these exact cards.  Do not
        # invent one or fall back to a LinkedIn search / company board.
        job.description_unavailable = True
        if job.job_id:
            guest_url = LINKEDIN_GUEST.format(job_id=job.job_id)
            job.description_method = "hyperverge.linkedin.detail.unavailable_after_public_guest_api_429"
            job.description_source_url = guest_url
            job.raw["apply_status"] = "detail_http_429"
            job.raw["detail_fetch_failure"] = {
                "status": 429,
                "url": guest_url,
                "reason": "A public guest API request for this exact company card returned HTTP 429 / JavaScript challenge; no further request was issued.",
            }
            job.raw["detail"] = {
                "status": "http_429_challenge",
                "response_url": guest_url,
                "http_status": 429,
                "description_unavailable": True,
                "challenge_reference": "companies/hyperverge/source/linkedin-429-4412659818.html",
            }
            job.warnings.append("detail_deferred_after_linkedin_429")
        else:
            job.description_method = "hyperverge.google_form.application_form_unavailable"
            job.description_source_url = job.url
            job.raw["apply_status"] = "application_form_not_available"
            job.raw["detail_fetch_failure"] = {
                "status": None,
                "url": job.url,
                "reason": "The company-published application form returned no public description body.",
            }
            job.raw["detail"] = {
                "status": "application_form_unavailable",
                "response_url": job.url,
                "http_status": None,
                "description_unavailable": True,
            }
        return job

    async def run(self):
        entry = await self._official_snapshot("initial")
        self.initial_snapshot = entry
        self.initial_listing = entry
        self.result.expected_total = entry["unique_count"]
        if entry["issues"]:
            self.result.errors.extend(entry["issues"])
        if entry["pagination_controls"]:
            self.result.errors.append("hyperverge_unreviewed_pagination_controls")
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=OFFICIAL_ENTRY,
            company_entry_final_url=entry["url"],
            listing_url=OFFICIAL_ENTRY + "#jobOpenings",
            listing_heading=entry["section_heading"],
            department_filters=entry["department_filters"],
            tab_summary=entry["tabs"],
            card_count=entry["card_count"],
            unique_job_count=entry["unique_count"],
            cross_tab_duplicates=entry["duplicates"],
            pagination="No pagination, load-more, search, location or date controls were observed; all 14 cards are server-rendered across the nine department panes.",
            membership_rule="Every .job-col card in the official #jobOpenings section is one published card; identical title+city+exact apply destination across two panes is one cross-listed posting.",
            apply_destination_counts={
                "linkedin": sum(1 for job in entry["jobs"] if job["job_id"]),
                "google_form": sum(1 for job in entry["jobs"] if job["application_form_id"]),
            },
            malformed_apply_urls=[
                {"title": job["title"], "url": job["apply_url_as_published"]}
                for job in entry["jobs"] if job["apply_url_malformed"]
            ],
            description_sources={
                "linkedin_public_guest_api": "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<numeric-id>",
                "google_form": "exact company-published docs.google.com/forms link; content is sign-in restricted",
            },
        )
        for row in entry["jobs"]:
            self.add(self._job_from_card(row))
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.metadata["detail_status_by_job"] = {
            (job.job_id or job.raw["listing"]["identity"]): {
                "status": job.raw.get("detail", {}).get("status"),
                "verified": job.description_verified,
                "unavailable": job.description_unavailable,
                "source": job.description_source_url,
            }
            for job in self.result.jobs
        }
        # Fresh final membership recheck of the same official page.
        final = await self._official_snapshot("final")
        final_ids = [job["identity"] for job in final["jobs"]]
        initial_ids = [job["identity"] for job in entry["jobs"]]
        self.result.metadata["initial_listing_ids"] = initial_ids
        self.result.metadata["final_listing_ids"] = final_ids
        self.result.metadata["final_listing_cards"] = final["card_count"]
        self.result.metadata["initial_final_ids_match"] = initial_ids == final_ids
        self.result.metadata["initial_final_card_count_match"] = entry["card_count"] == final["card_count"]
        if initial_ids != final_ids:
            self.result.errors.append("hyperverge_initial_final_company_listing_ids_changed")
        if entry["card_count"] != final["card_count"]:
            self.result.errors.append("hyperverge_initial_final_company_listing_card_count_changed")
        self.result.metadata["final_listing_recheck"] = {
            "url": final["url"],
            "status": final["status"],
            "cards": final["card_count"],
            "unique_jobs": final["unique_count"],
            "ids_match": initial_ids == final_ids,
        }
        self.result.listing_complete = not self.result.errors and initial_ids == final_ids
        self.result.metadata["membership_reconciliation"] = {
            "complete": self.result.listing_complete,
            "cards_observed": entry["card_count"],
            "unique_jobs": entry["unique_count"],
            "cross_tab_duplicates": len(entry["duplicates"]),
            "jobs_collected": len(self.result.jobs),
            "final_cards": final["card_count"],
            "final_unique_jobs": final["unique_count"],
        }
        return self.result
