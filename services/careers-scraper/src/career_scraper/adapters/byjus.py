"""BYJU'S official careers journey, including its live evergreen BDA intake.

Most department links on the current company page are broken or resolve to
unrelated learning articles. They do not establish a current global job list.
The exact company-linked Business Development Associates page is retained as an
evergreen recruitment route, with no fabricated requisition, posting date or
opening count.
"""

from copy import deepcopy
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from ..http import FetchError
from ..models import now, text_html
from .base import Adapter


ENTRY = "https://byjus.com/careers-at-byjus/"
CATEGORY_PREFIX = "/careers/all-openings/job-category/"
BUSINESS_DEVELOPMENT_LABEL = "Business Development Associates"
REQUIRED_ROLE_SECTIONS = ("Role description", "Skills", "Other details")


def _text(value):
    return " ".join((value or "").split())


def _is_byjus_url(value):
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in {"byjus.com", "www.byjus.com"}


def _fnv1a64(value):
    result = 14695981039346656037
    for character in _text(value):
        result = ((result ^ ord(character)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{result:016x}"


class Byjus(Adapter):
    VERIFICATION_REVISION = "byjus.official-careers-evergreen-and-broken-categories.1"

    def _last_capture(self, url):
        """Return this run's raw-response provenance after HTTP raises on 4xx."""
        if not self.http.output:
            return {}
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return {}
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("url") == url:
                return {key: record.get(key) for key in
                        ("final_url", "status", "sha256", "content_type", "redirects")}
        return {}

    async def _fetch(self, url, *, purpose):
        if not self.page_guard():
            record = {"url": url, "purpose": purpose, "outcome": "page_budget_exhausted"}
            self.result.metadata["journey"].append(record)
            return None, record
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            record = {
                "url": url, "purpose": purpose, "outcome": "request_failed",
                "error": str(exc), **self._last_capture(url),
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
            self.result.metadata["journey"].append(record)
            return None, record
        record = {
            "url": url, "purpose": purpose, "outcome": "received",
            "final_url": str(response.url), "status": response.status_code,
            "sha256": sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type"),
            "redirects": [str(item.url) for item in response.history],
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }
        self.result.metadata["journey"].append(record)
        return response, record

    def _entry_routes(self, response):
        soup = BeautifulSoup(response.text, "lxml")
        page_text = _text(soup.get_text(" ", strip=True))
        if "Join the Learning Tribe" not in page_text or "Browse through our departments" not in page_text:
            raise ValueError("BYJU'S official careers page content changed; current membership needs review")

        categories = []
        category_urls = set()
        bda_url = None
        bda_label = None
        view_all_jobs = None
        published_links = []
        for anchor in soup.select("a[href]"):
            target = urljoin(str(response.url), anchor["href"])
            label = _text(anchor.get_text(" ", strip=True))
            parsed = urlsplit(target)
            if not _is_byjus_url(target):
                continue
            published_links.append({"label": label, "url": target})
            if parsed.path == urlsplit(str(response.url)).path and parsed.fragment == "join-our-team":
                view_all_jobs = target
            if parsed.path.startswith(CATEGORY_PREFIX):
                if target not in category_urls:
                    category_urls.add(target)
                    categories.append({"label": label, "url": target})
            if parsed.path.rstrip("/") == "/apply" and BUSINESS_DEVELOPMENT_LABEL in label:
                bda_url = target
                bda_label = BUSINESS_DEVELOPMENT_LABEL
        return {
            "categories": categories, "bda_url": bda_url,
            "bda_label": bda_label, "view_all_jobs": view_all_jobs,
            "published_links": published_links,
        }

    @staticmethod
    def _is_not_found(response, record):
        if record.get("status") == 404:
            return True
        page_text = _text(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True)).lower()
        title = BeautifulSoup(response.text, "lxml").title
        title_text = _text(title.get_text(" ", strip=True)).lower() if title else ""
        return ("this page cannot be found" in page_text
                or "page not found" in title_text
                or "404 not found" in page_text)

    def _parse_evergreen_role(self, response, label):
        soup = BeautifulSoup(response.text, "lxml")
        container = soup.select_one(".apply-header")
        if container is None:
            raise ValueError("BYJU'S linked BDA page has no observed full role-details section")

        nodes = []
        section_text = None
        section_nodes = {name: [] for name in REQUIRED_ROLE_SECTIONS}
        for child in container.children:
            if not isinstance(child, Tag):
                continue
            classes = child.get("class", [])
            heading = _text(child.get_text(" ", strip=True))
            if "apply-video-section" in classes or heading.lower() in {"what does a bda do", "inside byju's sales team"}:
                break
            if child.name in {"form", "button", "script", "style", "noscript"}:
                continue
            nodes.append(deepcopy(child))
            if re.fullmatch(r"h[1-6]", child.name or ""):
                if heading in section_nodes:
                    section_text = heading
                elif heading != "Job Description":
                    raise ValueError(f"BYJU'S BDA page added an unreviewed role section: {heading}")
            elif section_text:
                section_nodes[section_text].append(child)

        headings = [
            _text(node.get_text(" ", strip=True)) for node in nodes
            if re.fullmatch(r"h[1-6]", node.name or "")
        ]
        expected_headings = ["Job Description", *REQUIRED_ROLE_SECTIONS]
        if headings != expected_headings:
            raise ValueError(f"BYJU'S BDA role sections changed: {headings!r}")
        sections = {
            name: text_html("".join(str(node) for node in section_nodes[name]))
            for name in REQUIRED_ROLE_SECTIONS
        }
        if any(not value for value in sections.values()):
            raise ValueError("BYJU'S BDA full role sections are incomplete")

        description_html = "".join(str(node) for node in nodes)
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            raise ValueError("BYJU'S BDA role body is too short to represent full details")

        article = soup.select_one("article[id]")
        compensation = sections["Other details"]
        application_button = next((button for button in soup.select("button")
                                   if _text(button.get_text(" ", strip=True)) == "Apply Now"), None)
        identity = sha256(f"byju-s|evergreen|{response.url}".encode()).hexdigest()
        job = self.job(
            {"record_type": "evergreen_recruitment_route",
             "listing": {"official_entry_url": ENTRY, "published_link_label": label,
                         "url": str(response.url), "requisition_id": None, "job_id": None},
             "detail": {"url": str(response.url), "sections": sections,
                        "description_html": description_html,
                        "page_content_identifier": article.get("id") if article else None,
                        "page_content_identifier_is_requisition": False,
                        "compensation_text": compensation},
             "application": {"control_label": _text(application_button.get_text(" ", strip=True))
                             if application_button else None,
                             "url": str(response.url), "submitted": False}},
            title=label,
            url=str(response.url),
            apply_url=str(response.url),
            description=description_html,
            method="byjus.official_bda_page_full_sections",
            description_url=str(response.url),
            department=label,
            responsibilities=sections["Role description"],
            qualifications=sections["Skills"],
            salary=None,
            record_identity=identity,
        )
        return job, {
            "title": label, "url": str(response.url),
            "record_type": "evergreen_recruitment_route",
            "job_id": None, "requisition_id": None, "date_posted": None,
            "body_characters": len(" ".join(description_text.split())),
            "rendered_reference_fnv1a64": _fnv1a64(description_text),
            "apply_now_observed": application_button is not None,
            "apply_submitted": False,
        }

    async def _review_category(self, category):
        url = category["url"]
        response, record = await self._fetch(url, purpose="official_department_category")
        reviewed = {"label": category["label"], "url": url, **record}
        if response is None:
            reviewed["classification"] = (
                "published_category_not_found" if record.get("status") == 404
                else "unavailable_published_category"
            )
            return reviewed
        final = urlsplit(str(response.url))
        if self._is_not_found(response, record):
            reviewed["classification"] = "published_category_not_found"
            return reviewed
        if final.hostname not in {"byjus.com", "www.byjus.com"}:
            reviewed["classification"] = "redirected_outside_company"
            return reviewed
        if final.path.rstrip("/") == "/sales-apply":
            text = _text(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True))
            reviewed.update(
                classification="evergreen_sales_application_funnel_not_opening_list",
                final_url=str(response.url),
                page_excerpt=text[:1800],
                posting_membership_verified=False,
            )
            return reviewed
        if str(response.url) != url:
            reviewed["classification"] = "redirected_to_unrelated_company_content"
            reviewed["final_url"] = str(response.url)
            reviewed["page_excerpt"] = _text(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True))[:800]
            return reviewed
        reviewed["classification"] = "category_membership_unverified"
        reviewed["page_excerpt"] = _text(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True))[:800]
        return reviewed

    async def run(self):
        started = time.perf_counter()
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            review_evidence="companies/byju-s/manual-reference.json",
            rendered_reference="companies/byju-s/browser-reference-parent.json",
            company_membership_verified=False,
            global_listing_membership_verified=False,
            expected_company_posting_count=None,
            exact_company_job_ids=[],
            ats_board_requests=0,
            journey=[], category_routes=[], evergreen_intakes=[],
        )

        entry_started = time.perf_counter()
        entry, _ = await self._fetch(self.company.careers_url, purpose="official_entry")
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
        if entry is None:
            self.result.errors.append("byju_s_official_entry_unavailable_membership_unknown")
            return self._finish(started)
        if not _is_byjus_url(str(entry.url)) or urlsplit(str(entry.url)).path.rstrip("/") != "/careers-at-byjus":
            self.result.errors.append("byju_s_official_entry_destination_changed_requires_membership_review")
            self.result.metadata["company_membership_verified"] = False
            return self._finish(started)
        try:
            routes = self._entry_routes(entry)
        except ValueError as exc:
            self.result.errors.append(str(exc))
            return self._finish(started)
        self.result.metadata.update(
            official_entry_final_url=str(entry.url),
            view_all_jobs_link=routes["view_all_jobs"],
            official_published_links=routes["published_links"],
            category_route_count=len(routes["categories"]),
            role_route_published=bool(routes["bda_url"]),
        )
        if not routes["categories"]:
            self.result.errors.append("byju_s_official_department_category_links_missing_membership_unknown")
        if not routes["bda_url"]:
            self.result.errors.append("byju_s_official_business_development_route_missing")

        detail_started = time.perf_counter()
        if routes["bda_url"]:
            if not _is_byjus_url(routes["bda_url"]) or urlsplit(routes["bda_url"]).path.rstrip("/") != "/apply":
                self.result.errors.append("byju_s_business_development_route_changed_requires_membership_review")
            else:
                detail, detail_record = await self._fetch(routes["bda_url"], purpose="official_business_development_evergreen_detail")
                if detail is None:
                    self.result.errors.append("byju_s_published_bda_evergreen_details_unavailable")
                else:
                    try:
                        job, intake = self._parse_evergreen_role(detail, routes["bda_label"])
                    except ValueError as exc:
                        self.result.errors.append(str(exc))
                    else:
                        self.add(job)
                        intake.update(detail_response_sha256=detail_record.get("sha256"))
                        self.result.metadata["evergreen_intakes"].append(intake)
        self.result.timings["detail_hydration_seconds"] = max(0.0, time.perf_counter() - detail_started)

        category_started = time.perf_counter()
        reviewed_routes = []
        for category in routes["categories"]:
            if not _is_byjus_url(category["url"]):
                reviewed_routes.append({"label": category["label"], "url": category["url"],
                                        "classification": "unverified_external_destination_not_followed"})
                continue
            reviewed_routes.append(await self._review_category(category))
        self.result.timings["category_review_seconds"] = max(0.0, time.perf_counter() - category_started)
        self.result.metadata["category_routes"] = reviewed_routes
        self.result.metadata["application_pipelines"] = [
            {"source_category_url": item["url"], "url": item.get("final_url"),
             "classification": item["classification"],
             "description": "Broad sales candidate intake route; not a numbered job list."}
            for item in reviewed_routes
            if item.get("classification") == "evergreen_sales_application_funnel_not_opening_list"
        ]
        self.result.metadata["category_membership_reconciled"] = bool(reviewed_routes) and all(
            item.get("classification") == "verified_job_listing" for item in reviewed_routes)
        if not self.result.metadata["category_membership_reconciled"]:
            self.result.errors.append("byju_s_official_category_membership_unresolved_broken_or_misdirected_routes")
        self.result.metadata["observed_posting_count"] = None
        self.result.metadata["numbered_postings_extracted"] = 0
        self.result.metadata["zero_openings_inferred"] = False
        self.result.metadata["evergreen_intake_count"] = len(self.result.metadata["evergreen_intakes"])
        self.result.metadata["collection_outcome"] = "partial_membership_unknown"
        return self._finish(started)

    def _finish(self, started):
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["company_collection_seconds"] = elapsed
        self.result.timings["source_collection_seconds"] = elapsed
        self.result.duration_seconds = elapsed
        self.result.finished_at = now()
        return self.result
