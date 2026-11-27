"""BlackRock's own server-rendered TalentBrew search + native detail pages.

Membership authority is the official unfiltered listing at
https://careers.blackrock.com/search-jobs (org 45831), paged with ?p=N.
Full descriptions come from .ats-description on each exact /job/... URL the
listing publishes. Workday apply links are retained per-job only; no Workday
board enumeration or universal ATS discovery is used.
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ORG_ID = "45831"
BASE = "https://careers.blackrock.com"
JOB_ROUTE = re.compile(r"^/job/[^/]+/[^/]+/45831/\d+$")
APPLY_HOSTS = {"blackrock.wd1.myworkdayjobs.com", "blackrock.tal.net"}
SALARY_RE = re.compile(
    r"salary range for this position is\s+([^\.\n]+?)" r"(?:\.|Additionally,)", re.I
)
SALARY_FULL = re.compile(
    r"(USD\$\s*[\d,]+\.?\d*\s*-\s*USD\$\s*[\d,]+\.?\d*)"
)
SALARY_LABEL_RE = re.compile(r"For\s+([^\n,]+(?:,[^\n]+)?)\s+Only\s+the\s+salary range", re.I)


def _parse_display_date(value):
    """Parse the visible 'Nov. 06, 2026' date to ISO; None if unparseable."""
    value = (value or "").strip()
    from datetime import datetime

    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%b. %d,%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _norm_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _salary_from_description(description_text):
    """Retain a prose salary range with its source label; no conversion."""
    for line in (description_text or "").splitlines():
        if "salary range for this position is" in line.lower():
            full = SALARY_FULL.search(line)
            if not full:
                continue
            label = SALARY_LABEL_RE.search(line)
            salary = {
                "source_text": _norm_text(line.strip()),
                "source_range": _norm_text(full.group(1)),
                "source_field": "ats-description",
            }
            if label:
                salary["source_location"] = _norm_text(label.group(1))
            salary["semantics"] = (
                "Source prose salary range retained verbatim; no currency conversion. "
                "Additional bonus/benefits language in the same sentence is part of the source text."
            )
            return salary
    return None


class BlackRock(Adapter):
    """Follow BlackRock's own paged search and exact native detail pages."""

    VERIFICATION_REVISION = "blackrock.talentbrew-search-and-native-details.1"
    CAREERS_HOST = "careers.blackrock.com"

    def _check_url(self, url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != self.CAREERS_HOST:
            raise ValueError(f"BlackRock page left its official host: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"BlackRock page has an unexpected path: {url}")
        return str(url)

    # ---- listing -----------------------------------------------------
    @classmethod
    def parse_listing_page(cls, html, page_url):
        soup = BeautifulSoup(html, "lxml")
        section = soup.select_one("section#search-results")
        if section is None:
            raise ValueError(f"BlackRock listing page {page_url} has no #search-results")
        try:
            total = int(section.get("data-total-job-results"))
            total_pages = int(section.get("data-total-pages"))
            current = int(section.get("data-current-page"))
            per_page = int(section.get("data-records-per-page"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"BlackRock listing page {page_url} lacks pagination attributes") from exc
        rows = []
        for li in soup.select(".section3__search-results-li"):
            anchor = li.select_one("a.section3__search-results-a")
            if anchor is None or not anchor.get("href") or not anchor.get("data-job-id"):
                raise ValueError(f"BlackRock listing page {page_url} has an unreadable row")
            href = anchor.get("href")
            job_id = anchor.get("data-job-id")
            if not JOB_ROUTE.fullmatch(urlsplit(href).path):
                raise ValueError(f"BlackRock listing row has an unexpected detail route: {href}")
            if urlsplit(href).path.endswith(f"/{job_id}") is False:
                raise ValueError(f"BlackRock listing row ID does not match its detail route: {href}")
            title_el = li.select_one(".section3__job-title")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title:
                raise ValueError(f"BlackRock listing row {job_id} has no title")
            info = {}
            for span in li.select(".section3__job-information"):
                text = _norm_text(span.get_text(" ", strip=True))
                for label in ("Location:", "Additional Locations:", "Team:"):
                    if text.startswith(label):
                        info[label.rstrip(":")] = text[len(label):].strip()
                        break
            if not info.get("Location"):
                raise ValueError(f"BlackRock listing row {job_id} has no location")
            rows.append({
                "job_id": job_id,
                "href": href,
                "title": title,
                "location": info.get("Location"),
                "additional_locations": info.get("Additional Locations"),
                "team": info.get("Team"),
                "page_url": page_url,
            })
        return rows, {"total": total, "total_pages": total_pages, "current": current,
                      "per_page": per_page}

    # ---- detail ------------------------------------------------------
    @classmethod
    def parse_detail(cls, html, *, expected_job_id, listing_row, detail_url):
        parsed_url = urlsplit(str(detail_url))
        if (parsed_url.scheme != "https" or parsed_url.hostname != "careers.blackrock.com"
                or not JOB_ROUTE.fullmatch(parsed_url.path)
                or parsed_url.path.rsplit("/", 1)[-1] != expected_job_id):
            raise ValueError(f"BlackRock detail URL does not match expected job ID {expected_job_id}: {detail_url}")
        soup = BeautifulSoup(html, "lxml")
        heading = (soup.select_one(".section4__heading")
                   or soup.select_one(".ajd_header__job-title")
                   or soup.select_one("h1"))
        if heading is None:
            raise ValueError(f"BlackRock detail {detail_url} has no job heading")
        title = heading.get_text(" ", strip=True)
        if not title:
            raise ValueError(f"BlackRock detail {detail_url} has an empty job heading")
        info = {}
        for selector in (".section4__job-info", ".section5__job-info", ".ajd_header__location",
                         ".ajd_header__job-info"):
            for span in soup.select(selector):
                text = _norm_text(span.get_text(" ", strip=True))
                for label in ("Location:", "Locations:", "Additional Locations:", "Team:",
                              "Job Requisition #:", "Job Requisition #", "Date posted:"):
                    if text.startswith(label):
                        key = label.rstrip(" #:")
                        if key not in info:
                            info[key] = text[len(label):].strip()
                        break
        # AJD pages carry req + date in JSON-LD / meta when visible blocks omit them.
        req_meta = soup.select_one('meta[name="job-ats-req-id"]')
        if req_meta is not None and "Job Requisition" not in info:
            info["Job Requisition"] = req_meta.get("content", "").strip() or None
        desc_el = soup.select_one(".ats-description")
        if desc_el is None:
            raise ValueError(f"BlackRock detail {expected_job_id} has no .ats-description")
        description_html = desc_el.decode_contents()
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            raise ValueError(f"BlackRock detail {expected_job_id} description is implausibly short")
        # JSON-LD corroboration (twin of .ats-description, HTML-escaped there)
        ld = None
        ld_el = soup.select_one('script[type="application/ld+json"]')
        if ld_el is not None and ld_el.string:
            try:
                ld = json.loads(ld_el.string)
            except ValueError:
                ld = None
        req = info.get("Job Requisition #")
        if ld is not None:
            if ld.get("title") != title:
                raise ValueError(f"BlackRock detail {expected_job_id} JSON-LD title differs")
            if ld.get("url") != detail_url:
                raise ValueError(f"BlackRock detail {expected_job_id} JSON-LD url differs")
            ld_req = (ld.get("identifier") or {}).get("value")
            if req is None:
                req = ld_req
            elif ld_req is not None and ld_req != req:
                raise ValueError(f"BlackRock detail {expected_job_id} req mismatch: {req} vs {ld_req}")
        # Apply link: Workday per-job URL, must carry the req ID
        apply_anchors = [a for a in soup.select("a.job-apply")
                         if a.get("data-apply-url") or a.get("href")]
        if not apply_anchors:
            raise ValueError(f"BlackRock detail {expected_job_id} has no apply link")
        apply_url = apply_anchors[0].get("data-apply-url") or apply_anchors[0].get("href")
        apply_parsed = urlsplit(apply_url)
        if apply_parsed.scheme != "https" or apply_parsed.hostname not in APPLY_HOSTS:
            raise ValueError(f"BlackRock detail {expected_job_id} apply link left the inspected issuers")
        if apply_parsed.hostname == "blackrock.wd1.myworkdayjobs.com" and req and req not in apply_url:
            raise ValueError(f"BlackRock detail {expected_job_id} apply link does not carry its req ID")
        if apply_parsed.hostname == "blackrock.tal.net" and "/opp/" not in apply_parsed.path:
            raise ValueError(f"BlackRock detail {expected_job_id} TAL.NET apply link lacks an opp ID")
        date_posted_raw = info.get("Date posted")
        ld_date = ld.get("datePosted") if ld else None
        date_posted = ld_date or _parse_display_date(date_posted_raw)
        salary = _salary_from_description(description_text)
        locations = [{"location": info.get("Location") or info.get("Locations")}]
        if info.get("Additional Locations"):
            locations.append({"location": info["Additional Locations"], "additional": True})
        if listing_row and listing_row.get("additional_locations"):
            already = {loc.get("location") for loc in locations}
            if listing_row["additional_locations"] not in already:
                locations.append({"location": listing_row["additional_locations"],
                                  "additional_listing": True})
        org = (ld.get("hiringOrganization") or {}).get("name") if ld else None
        address = None
        if ld and ld.get("jobLocation"):
            address = (ld["jobLocation"][0].get("address") or {})
        return {
            "title": title,
            "description_html": description_html,
            "description_text": description_text,
            "requisition_id": req,
            "apply_url": apply_url,
            "info": info,
            "date_posted": date_posted,
            "date_posted_raw": date_posted_raw,
            "jsonld": ld,
            "locations": locations,
            "team": info.get("Team") or (listing_row or {}).get("team"),
            "salary": salary,
            "employer": org or "BlackRock",
            "address": address,
            "detail_url": detail_url,
        }

    def _make_job(self, row, detail):
        detail_url = urljoin(BASE + "/", row["href"]) if not row["href"].startswith("http") else row["href"]
        raw = {
            "listing": row,
            "listing_page_url": row["page_url"],
            "detail": {
                "info": detail["info"],
                "jsonld": detail["jsonld"],
                "address": detail["address"],
                "apply_url": detail["apply_url"],
            },
            "source_ui": {
                "listing_href": row["href"],
                "listing_location": row["location"],
                "listing_additional_locations": row["additional_locations"],
                "listing_team": row["team"],
                "detail_url": detail_url,
                "attachment_links": _job_attachment_links(detail["description_html"]),
                "salary_from_description": detail["salary"],
            },
        }
        job = self.job(
            raw,
            title=detail["title"],
            url=detail_url,
            job_id=row["job_id"],
            requisition_id=detail["requisition_id"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="blackrock.native_job_page.ats-description",
            description_url=detail_url,
            locations=detail["locations"],
            department=None,
            team=detail["team"],
            employment_type=None,
            workplace_type=None,
            date_posted=detail["date_posted"],
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            salary=detail["salary"],
            employer=detail["employer"],
        )
        job.description_text = detail["description_text"]
        job.record_identity = f"blackrock:{row['job_id']}"
        if detail["title"] != row["title"]:
            job.warnings.append("listing_title_differs_from_detail_title")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname != self.CAREERS_HOST
                or entry_parts.path.rstrip("/") != "/search-jobs"):
            raise ValueError("BlackRock dedicated flow must start from its official /search-jobs entry")

        # ---- listing: all pages, no filters, org 45831 ----
        listing_started = time.perf_counter()
        page1_url = entry_url
        rows_by_id = {}
        page_order = []
        first_meta = None
        first_response = await self.http.get(page1_url)
        self._check_url(first_response.url, path_prefix="/search-jobs")
        page_rows, first_meta = self.parse_listing_page(first_response.text, page1_url)
        total, total_pages, per_page = first_meta["total"], first_meta["total_pages"], first_meta["per_page"]
        if total <= 0 or total_pages <= 0 or per_page <= 0:
            raise ValueError("BlackRock listing reports zero jobs; independent review required")
        all_pages = [(1, page1_url, page_rows)]
        for page in range(2, total_pages + 1):
            if not self.page_guard():
                self.result.errors.append("blackrock_page_budget_exhausted")
                break
            url = f"{BASE}/search-jobs?p={page}"
            response = await self.http.get(url)
            self._check_url(response.url, path_prefix="/search-jobs")
            page_rows, meta = self.parse_listing_page(response.text, url)
            if meta["current"] != page or meta["total"] != total or meta["total_pages"] != total_pages:
                raise ValueError(f"BlackRock page {page} pagination/total changed mid-run")
            all_pages.append((page, url, page_rows))
        for page, url, page_rows in all_pages:
            for row in page_rows:
                if row["job_id"] in rows_by_id:
                    raise ValueError(f"BlackRock listing repeats job ID {row['job_id']}")
                rows_by_id[row["job_id"]] = row
                page_order.append(row["job_id"])
        pages_reconciled = {page: len(page_rows) for page, _, page_rows in all_pages}
        if len(rows_by_id) != total:
            self.result.errors.append(
                f"blackrock_listing_count_mismatch: expected {total}, unique rows {len(rows_by_id)}")
        # terminal page + past-end confirmation (bounded: only when total>per_page)
        terminal_size = pages_reconciled.get(total_pages, 0)
        expected_last = total - per_page * (total_pages - 1)
        if terminal_size != expected_last:
            self.result.errors.append(
                f"blackrock_terminal_page_mismatch: expected {expected_last}, got {terminal_size}")
        listing_seconds = time.perf_counter() - listing_started

        # ---- details: fetch every exact published URL ----
        detail_started = time.perf_counter()

        async def detail_for(job):
            url = job.raw["source_ui"]["detail_url"]
            response = await self.http.get(url)
            self._check_url(response.url, path_prefix="/job/")
            parsed = self.parse_detail(
                response.text, expected_job_id=job.job_id,
                listing_row=job.raw["listing"], detail_url=str(response.url))
            if str(response.url) != url:
                raise ValueError(f"BlackRock detail {job.job_id} redirected")
            fresh = self._make_job(job.raw["listing"], parsed)
            fresh.description_verified = job.description_verified  # keep hydrate accounting stable
            return fresh

        listings = [rows_by_id[jid] for jid in page_order]
        pending = []
        for row in listings:
            detail_url = urljoin(BASE + "/", row["href"]) if not row["href"].startswith("http") else row["href"]
            pending.append(self.job({"listing": row, "source_ui": {"detail_url": detail_url}},
                                    title=row["title"], url=detail_url, job_id=row["job_id"]))
            pending[-1].record_identity = f"blackrock:{row['job_id']}"
        hydrated = await self.hydrate(pending, detail_for)
        for job in hydrated:
            self.add(job)
        detail_seconds = time.perf_counter() - detail_started

        self.result.expected_total = total
        self.result.pages = len(all_pages)
        self.result.listing_complete = len(rows_by_id) == total and not any(
            e.startswith(("blackrock_listing_count_mismatch", "blackrock_terminal_page_mismatch",
                          "blackrock_page_budget_exhausted")) for e in self.result.errors)
        failed_details = [e for e in self.result.errors if e.startswith("Detail ")]
        if failed_details:
            self.result.listing_complete = False
        if any(job.warnings for job in self.result.jobs):
            pass  # warnings are per-job annotations, not fatal by themselves
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": page1_url,
            "membership_authority": "BlackRock official /search-jobs unfiltered listing (org 45831, TalentBrew server-rendered)",
            "org_id": ORG_ID,
            "listing_filter_behavior": "No keywords/location/facet filters applied; default Relevancy sort; facet checkboxes ignored",
            "ui_pagination": "Server-rendered ?p=N pages, 10 rows per page",
            "total": total,
            "total_pages": total_pages,
            "records_per_page": per_page,
            "page_row_counts": pages_reconciled,
            "listed_job_ids_in_order": page_order,
            "description_source": ".ats-description inner HTML on each exact published /job/... page; JSON-LD JobPosting retained as corroboration",
            "salary_policy": "Prose salary ranges retained verbatim from description; no conversion; empty baseSalary stays null",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "listing_phase_seconds": round(listing_seconds, 6),
            "detail_phase_wall_seconds": round(detail_seconds, 6),
            "detail_failures": failed_details,
        })
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        return self.result
