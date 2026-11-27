"""Chargebee's company-published LinkedIn Jobs route and exact listed job pages."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import gzip
import json
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..http import FetchError
from ..models import Source, now


ENTRY = "https://www.chargebee.com/careers/"
COMPANY_CAREERS = "https://www.chargebee.com/company/careers/"
GLOBAL_PROFILE_PATH = "/company/chargebee/jobs/"
INDIA_PROFILE_PATH = "/company/chargebee/jobs/"
LINKEDIN_HOSTS = {"www.linkedin.com", "in.linkedin.com", "ie.linkedin.com"}
CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/132.0.0.0 Safari/537.36"
)
LINKEDIN_HEADERS = {
    "User-Agent": CHROME_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_RENDERED_BLOCK_TAGS = frozenset({
    "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
})


def _soup(response_text: str) -> BeautifulSoup:
    return BeautifulSoup(response_text, "lxml")


def _link_text(anchor) -> str:
    return " ".join((anchor.get_text(" ", strip=True) or anchor.get("aria-label", "")).split())


def _company_profile_links(soup: BeautifulSoup):
    global_links = []
    india_links = []
    for anchor in soup.select("a[href]"):
        label = _link_text(anchor)
        url = urljoin(COMPANY_CAREERS, anchor["href"])
        parsed = urlsplit(url)
        if parsed.path.rstrip("/") != GLOBAL_PROFILE_PATH.rstrip("/"):
            continue
        if parsed.hostname == "www.linkedin.com" and "Explore Opportunities" in label:
            global_links.append((label, url))
        elif parsed.hostname == "in.linkedin.com" and "Explore Opportunities" in label:
            india_links.append((label, url))
    if len(global_links) != 1 or len(india_links) != 1:
        raise ValueError("Chargebee official page must expose one global and one India LinkedIn jobs route")
    return global_links[0], india_links[0]


def _see_all_jobs(soup: BeautifulSoup, *, page_url: str):
    matches = []
    for anchor in soup.select("a[href]"):
        label = _link_text(anchor).lower()
        url = urljoin(page_url, anchor["href"])
        parsed = urlsplit(url)
        if label == "see all jobs" and parsed.hostname in LINKEDIN_HOSTS and parsed.path == "/jobs/chargebee-jobs-worldwide":
            matches.append((label, url))
    if len(matches) != 1:
        raise ValueError("Chargebee LinkedIn profile must expose one exact See all jobs route")
    return matches[0]


def _company_filter_id(url: str) -> str:
    parsed = urlsplit(url)
    values = parse_qs(parsed.query).get("f_C", [])
    if (parsed.hostname not in LINKEDIN_HOSTS or parsed.path != "/jobs/chargebee-jobs-worldwide"
            or len(values) != 1 or not values[0].isdigit()):
        raise ValueError("Chargebee published LinkedIn See all jobs URL has an unknown company filter")
    return values[0]


def _linkedin_company_list_key(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.path, _company_filter_id(url)


def _stable_listing_row(row: dict) -> dict:
    stable = dict(row)
    parsed = urlsplit(stable.get("job_url", ""))
    stable["job_url"] = parsed.path
    stable.pop("posted_text", None)
    attributes = dict(stable.get("source_card_attributes", {}))
    for key in ("data-reference-id", "data-tracking-id", "data-impression-id"):
        attributes.pop(key, None)
    stable["source_card_attributes"] = attributes
    return stable


def _listing_total(soup: BeautifulSoup) -> int:
    h1 = " ".join((soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "").split())
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1_match = re.search(r"^(\d+)\s+Chargebee Jobs in Worldwide$", h1, re.I)
    title_match = re.search(r"\((\d+)\s+Open Roles\)", title, re.I)
    if not h1_match or not title_match or h1_match.group(1) != title_match.group(1):
        raise ValueError("Chargebee LinkedIn worldwide page is missing a consistent public results count")
    total = int(h1_match.group(1))
    if total <= 0:
        raise ValueError("Chargebee public LinkedIn listing returned a zero result count; do not certify a false zero")
    return total


def _listing_rows(soup: BeautifulSoup, *, page_url: str) -> tuple[list[dict], int, list[str]]:
    total = _listing_total(soup)
    rows = []
    for card in soup.select("div.base-card[data-entity-urn]"):
        identity_match = re.fullmatch(r"urn:li:jobPosting:(\d+)", card.get("data-entity-urn", ""))
        anchor = card.select_one("a.base-card__full-link[href]")
        title = card.select_one(".base-search-card__title") or card.find("h3")
        company = card.select_one(".base-search-card__subtitle") or card.find("h4")
        location = card.select_one(".job-search-card__location")
        posted = card.select_one(".job-search-card__listdate")
        if not identity_match or not anchor or not title or not company:
            raise ValueError("Chargebee LinkedIn listing card is missing its public id/title/company/link")
        identity = identity_match.group(1)
        url = urljoin(page_url, anchor["href"])
        parsed = urlsplit(url)
        path_identity = re.search(r"-(\d+)$", parsed.path.rstrip("/"))
        if (parsed.hostname not in LINKEDIN_HOSTS or not parsed.path.startswith("/jobs/view/")
                or not path_identity or path_identity.group(1) != identity):
            raise ValueError(f"Chargebee listed posting {identity} has a mismatched LinkedIn detail URL")
        company_name = " ".join(company.get_text(" ", strip=True).split())
        if company_name != "Chargebee":
            raise ValueError(f"LinkedIn result {identity} has foreign company name {company_name!r}")
        position = card.get("data-row")
        if position is None:
            query_position = parse_qs(parsed.query).get("position", [])
            position = query_position[0] if query_position else None
        page_num = parse_qs(parsed.query).get("pageNum", [])
        if len(page_num) != 1 or page_num[0] != "0":
            raise ValueError(f"Chargebee result {identity} is outside the observed single public listing page")
        rows.append({
            "job_id": identity,
            "title": " ".join(title.get_text(" ", strip=True).split()),
            "company": company_name,
            "location": " ".join(location.get_text(" ", strip=True).split()) if location else None,
            "job_url": url,
            "position": int(position) if str(position or "").isdigit() else None,
            "page_num": page_num[0],
            "posted_text": " ".join(posted.get_text(" ", strip=True).split()) if posted else None,
            "date_posted_raw": posted.get("datetime") if posted else None,
            "badges": [" ".join(item.get_text(" ", strip=True).split()) for item in card.select(".job-posting-benefits")],
            "source_card_attributes": {key: value for key, value in card.attrs.items() if key.startswith("data-")},
        })
    page_numbers = sorted({row["page_num"] for row in rows})
    if len(rows) != total or len({row["job_id"] for row in rows}) != len(rows):
        raise ValueError(f"Chargebee LinkedIn listing count/unique IDs do not match: count={total}, cards={len(rows)}")
    positions = [row["position"] for row in rows]
    if positions != list(range(1, total + 1)):
        raise ValueError("Chargebee LinkedIn page positions are missing, repeated, or out of order")
    pagination = []
    for anchor in soup.select("a[href]"):
        target = urljoin(page_url, anchor["href"])
        query = parse_qs(urlsplit(target).query)
        label = _link_text(anchor).lower()
        if ("start" in query or (query.get("pageNum") and query["pageNum"] != ["0"])
                or label in {"next", "next page", "previous", "previous page"}):
            pagination.append(target)
    if pagination or page_numbers != ["0"]:
        raise ValueError("Chargebee LinkedIn listing indicates additional pages; current collection must reconcile them first")
    return rows, total, pagination


def _posted_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _criteria(soup: BeautifulSoup) -> list[dict]:
    output = []
    for item in soup.select("li.description__job-criteria-item"):
        label = item.select_one("h3.description__job-criteria-subheader")
        value = item.select_one("span.description__job-criteria-text")
        if label and value:
            output.append({
                "label": " ".join(label.get_text(" ", strip=True).split()),
                "value": " ".join(value.get_text(" ", strip=True).split()),
            })
    return output


def _criteria_dict(values: list[dict]) -> dict[str, str]:
    result = {}
    for item in values:
        label = item["label"]
        if label in result:
            result[label] = f"{result[label]}; {item['value']}"
        else:
            result[label] = item["value"]
    return result


def _description_html(soup: BeautifulSoup) -> str:
    markup = soup.select_one(".show-more-less-html__markup")
    if not markup:
        return ""
    return "".join(str(child) for child in markup.contents)


def _rendered_text_from_html(fragment: str | None) -> str:
    """Match rendered CUA innerText while preserving inline text nodes."""
    soup = BeautifulSoup(fragment or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    pieces: list[str] = []

    def add_break() -> None:
        if pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            pieces.append(re.sub(r"\s+", " ", str(node)))
            return
        if not isinstance(node, Tag):
            return
        if node.name in {"script", "style", "noscript"}:
            return
        if node.name == "br":
            add_break()
            return
        block = node.name in _RENDERED_BLOCK_TAGS
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


def _compact_hash(value: str) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    return sha256(compact.encode()).hexdigest()


class Chargebee(Adapter):
    """Chargebee official careers page -> its linked LinkedIn company jobs list."""

    VERIFICATION_REVISION = "chargebee.official-careers-linkedin-worldwide.1"

    def __init__(self, http, company, source: Source, options):
        super().__init__(http, company, source, options)
        self.linkedin_headers = dict(LINKEDIN_HEADERS)
        self.linkedin_global_profile_url = None
        self.linkedin_india_profile_url = None
        self.linkedin_worldwide_url = None
        self.company_filter_id = None
        self.preloaded_detail_pages: dict[str, dict] = {}
        self._reused_detail_ids: list[str] = []
        self._reused_logged_ids: set[str] = set()
        self._published_ids: set[str] = set()
        self._linkedin_429: dict | None = None
        self._rate_limit_deferred_ids: list[str] = []
        self.source_probe_seconds = 0.0

    async def _official_journey(self):
        started = time.perf_counter()
        entry = await self.http.get(ENTRY, headers=self.linkedin_headers)
        if (urlsplit(str(entry.url)).hostname != "www.chargebee.com"
                or urlsplit(str(entry.url)).path.rstrip("/") != "/company/careers"):
            raise ValueError("Chargebee careers entry no longer reaches the observed company careers page")
        careers_soup = _soup(entry.text)
        if "chargebee" not in careers_soup.get_text(" ", strip=True).lower():
            raise ValueError("Chargebee careers page no longer identifies its company brand")
        (global_label, global_profile), (india_label, india_profile) = _company_profile_links(careers_soup)

        global_response = await self.http.get(global_profile, headers=self.linkedin_headers)
        india_response = await self.http.get(india_profile, headers=self.linkedin_headers)
        if (urlsplit(str(global_response.url)).hostname not in LINKEDIN_HOSTS
                or urlsplit(str(global_response.url)).path.rstrip("/") != GLOBAL_PROFILE_PATH.rstrip("/")):
            raise ValueError("Chargebee global LinkedIn company profile route changed")
        if (urlsplit(str(india_response.url)).hostname not in LINKEDIN_HOSTS
                or urlsplit(str(india_response.url)).path.rstrip("/") != INDIA_PROFILE_PATH.rstrip("/")):
            raise ValueError("Chargebee India LinkedIn company profile route changed")
        global_see_all_label, global_all = _see_all_jobs(_soup(global_response.text), page_url=str(global_response.url))
        india_see_all_label, india_all = _see_all_jobs(_soup(india_response.text), page_url=str(india_response.url))
        global_filter = _company_filter_id(global_all)
        india_filter = _company_filter_id(india_all)
        if global_filter != india_filter:
            raise ValueError("Chargebee global and India See all jobs links no longer share one exact company filter")
        self.linkedin_global_profile_url = global_profile
        self.linkedin_india_profile_url = india_profile
        self.linkedin_worldwide_url = global_all
        self.company_filter_id = global_filter

        global_preview = self._profile_preview(_soup(global_response.text))
        india_preview = self._profile_preview(_soup(india_response.text))
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            company_careers_final_url=str(entry.url),
            company_global_jobs_cta={"label": global_label, "url": global_profile},
            company_india_jobs_cta={"label": india_label, "url": india_profile},
            global_profile_see_all={"label": global_see_all_label, "url": global_all},
            india_profile_see_all={"label": india_see_all_label, "url": india_all},
            company_filter_id=global_filter,
            profile_preview_counts={"global": len(global_preview), "india": len(india_preview)},
            profile_preview_ids={
                "global": [item["job_id"] for item in global_preview],
                "india": [item["job_id"] for item in india_preview],
            },
            profile_preview_evidence={
                "global_cards": len(global_preview), "india_cards": len(india_preview),
                "sign_in_overlay_text_present": {
                    "global": any(text in global_response.text for text in ("Agree & Join LinkedIn", "Sign in to see who")),
                    "india": any(text in india_response.text for text in ("Agree & Join LinkedIn", "Sign in to see who")),
                },
                "note": "Profile previews/guest sign-in overlays do not define membership; the company-published See all jobs route is authoritative.",
            },
            listing_request_shape={
                "method": "GET",
                "url": global_all,
                "company_filter_parameter": {"f_C": global_filter},
                "keyword_location_date_filters": "none in the company-published See all jobs URL",
                "pagination": "LinkedIn server-rendered global company page exposes all returned roles with pageNum=0",
            },
            official_scope_seconds=round(time.perf_counter() - started, 6),
        )
        self.result.timings["official_scope_source_seconds"] = round(time.perf_counter() - started, 6)
        return global_preview, india_preview

    @staticmethod
    def _profile_preview(soup: BeautifulSoup) -> list[dict]:
        rows = []
        for card in soup.select("div.base-card[data-entity-urn]"):
            match = re.fullmatch(r"urn:li:jobPosting:(\d+)", card.get("data-entity-urn", ""))
            title = card.select_one(".base-search-card__title") or card.find("h3")
            anchor = card.select_one("a.base-card__full-link[href]")
            if match and title and anchor:
                rows.append({"job_id": match.group(1), "title": " ".join(title.get_text(" ", strip=True).split()),
                             "url": urljoin("https://www.linkedin.com", anchor["href"])})
        return rows

    async def _listing_page(self, *, phase: str):
        if not self.page_guard():
            raise ValueError("Chargebee LinkedIn pagination page budget exhausted")
        started = time.perf_counter()
        response = await self.http.get(self.linkedin_worldwide_url, headers=self.linkedin_headers)
        final_url = str(response.url)
        parsed = urlsplit(final_url)
        if (parsed.hostname not in LINKEDIN_HOSTS or parsed.path != "/jobs/chargebee-jobs-worldwide"
                or parse_qs(parsed.query).get("f_C") != [self.company_filter_id]):
            raise ValueError("Chargebee exact worldwide listing redirected outside its published company filter")
        rows, total, pagination = _listing_rows(_soup(response.text), page_url=final_url)
        self.result.timings[f"{phase}_listing_seconds"] = round(time.perf_counter() - started, 6)
        snapshot = {
            "phase": phase,
            "url": final_url,
            "total": total,
            "cards": len(rows),
            "page_nums": sorted({row["page_num"] for row in rows}),
            "positions": [row["position"] for row in rows],
            "ids": [row["job_id"] for row in rows],
            "url_by_id": {row["job_id"]: row["job_url"] for row in rows},
            "stable_row_sha256": {row["job_id"]: _compact_hash(json.dumps(_stable_listing_row(row), ensure_ascii=False, sort_keys=True)) for row in rows},
            "full_html_sha256": sha256(response.content).hexdigest(),
            "pagination_links": pagination,
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        return rows, total, snapshot

    def _make_job(self, row: dict):
        employer = row.get("company") or "Chargebee"
        job = self.job(
            {"listing": row}, title=row["title"], job_id=row["job_id"], url=row["job_url"],
            date_posted=_posted_date(row.get("date_posted_raw")),
            date_posted_raw=row.get("date_posted_raw"), employer=employer,
            locations=[row["location"]] if row.get("location") else [],
        )
        return job

    @staticmethod
    def _same_detail_path(current_url: str, cached_url: str, job_id: str) -> bool:
        current, cached = urlsplit(current_url), urlsplit(cached_url)
        suffix = re.search(r"-(\d+)$", current.path.rstrip("/"))
        cached_suffix = re.search(r"-(\d+)$", cached.path.rstrip("/"))
        return bool(
            current.hostname in LINKEDIN_HOSTS and cached.hostname in LINKEDIN_HOSTS
            and current.path == cached.path and suffix and cached_suffix
            and suffix.group(1) == cached_suffix.group(1) == str(job_id)
        )

    def _archive_reused_detail(self, identity: str, cached: dict):
        if not self.http.output:
            return
        if identity in self._reused_logged_ids:
            return
        body = cached["body"]
        digest = sha256(body).hexdigest()
        if digest != cached["sha256"]:
            raise ValueError(f"Chargebee reused detail response hash mismatch for {identity}")
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(body))
        record = {
            "url": cached["requested_url"], "final_url": cached["final_url"],
            "method": "GET", "request_body": None, "status": 200,
            "sha256": digest, "content_type": cached.get("content_type"), "redirects": cached.get("redirects", []),
            "fetched_at": cached["fetched_at"], "reused_probe_response": True,
            "evidence_scope": f"exact currently published Chargebee job {identity}; same path revalidated in listing snapshot",
            "reused_from": cached.get("source_file"),
        }
        requests_path = self.http.output / "requests.jsonl"
        with requests_path.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._reused_logged_ids.add(identity)

    def _detail_from_html(self, job, page_html: str, response_url: str, response_meta: dict | None = None):
        soup = _soup(page_html)
        title_element = soup.select_one("h1.top-card-layout__title")
        title = " ".join(title_element.get_text(" ", strip=True).split()) if title_element else ""
        company_link = soup.select_one("a.topcard__org-name-link")
        company_name = " ".join(company_link.get_text(" ", strip=True).split()) if company_link else None
        top_card = soup.select_one(".top-card-layout__second-subline")
        location_element = top_card.select_one(".topcard__flavor-row .topcard__flavor--bullet") if top_card else None
        location = " ".join(location_element.get_text(" ", strip=True).split()) if location_element else None
        posted = soup.select_one(".top-card-layout__second-subline .posted-time-ago__text")
        posted_text = " ".join(posted.get_text(" ", strip=True).split()) if posted else None
        applicants = soup.select_one(".top-card-layout__second-subline .num-applicants__caption")
        criteria_rows = _criteria(soup)
        criteria = _criteria_dict(criteria_rows)
        body_html = _description_html(soup)
        body_text = _rendered_text_from_html(body_html)
        source_url = str(response_url)
        if not self._same_detail_path(job.url, source_url, str(job.job_id)):
            raise ValueError(f"Chargebee exact detail {job.job_id} response URL does not match the listed path")
        if title != job.title or company_name != "Chargebee":
            raise ValueError(f"Chargebee exact detail {job.job_id} title/company does not match its current listing")
        if len(body_text.strip()) < 100:
            raise ValueError(f"Chargebee exact detail {job.job_id} does not contain a full public description")

        apply_buttons = [
            {"text": " ".join(button.get_text(" ", strip=True).split()), "attributes": dict(button.attrs)}
            for button in soup.select("button.apply-button")
        ]
        apply_anchors = [
            {"text": " ".join(anchor.get_text(" ", strip=True).split()), "url": urljoin(source_url, anchor["href"])}
            for anchor in soup.select("a.apply-button[href]")
        ]
        direct_apply_urls = [
            item["url"] for item in apply_anchors
            if urlsplit(item["url"]).hostname not in LINKEDIN_HOSTS
            or not urlsplit(item["url"]).path.startswith("/login")
        ]
        description_markup = soup.select_one(".show-more-less-html__markup")
        description_links = [
            {"url": urljoin(source_url, anchor["href"]), "text": anchor.get_text(" ", strip=True)}
            for anchor in (description_markup.select("a[href]") if description_markup else [])
        ]
        meta = {}
        for name in ("description", "og:title", "og:description", "twitter:title", "twitter:description"):
            tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
            if tag and tag.get("content"):
                meta[name] = tag["content"]
        language = (soup.html.get("lang") if soup.html else None) or "en"
        job.raw["detail"] = {
            "response_url": source_url,
            "title": title,
            "company": company_name,
            "location": location,
            "relative_posted_text": posted_text,
            "applicant_text": applicants.get_text(" ", strip=True) if applicants else None,
            "job_criteria": criteria_rows,
            "description_html": body_html,
            "description_links": description_links,
            "apply_buttons": apply_buttons,
            "apply_anchors": apply_anchors,
            "meta": meta,
            "language": language,
            "response_sha256": response_meta.get("sha256") if response_meta else None,
            "source_fetched_at": response_meta.get("fetched_at") if response_meta else None,
            "reused_probe_response": bool(response_meta and response_meta.get("reused_probe_response")),
        }
        job.raw["description_links"] = description_links
        job.description_html = body_html
        job.description_text = body_text
        job.description_method = "chargebee.linkedin.company_published_job_detail.description__markup"
        job.description_source_url = source_url
        job.employer = company_name
        job.locations = [location] if location else []
        job.department = criteria.get("Job function")
        job.employment_type = criteria.get("Employment type")
        job.workplace_type = criteria.get("Workplace type") or criteria.get("Work mode")
        job.experience = criteria.get("Seniority level")
        job.language = language
        salary = next((value for label, value in criteria.items()
                       if re.search(r"salary|compensation|pay", label, re.I)), None)
        job.salary = salary
        # The public LinkedIn page exposes Apply as a button with tracking attributes but no URL.
        job.apply_url = direct_apply_urls[0] if len(direct_apply_urls) == 1 else None
        job.requisition_id = None
        job.date_updated = None
        job.valid_through = None
        job.qualifications = None
        job.responsibilities = None
        job.education = None
        if job.date_posted is None:
            job.date_posted = _posted_date(job.raw["listing"].get("date_posted_raw"))
        if job.date_posted_raw is None:
            job.date_posted_raw = job.raw["listing"].get("date_posted_raw")
        listing_location = job.raw["listing"].get("location")
        if listing_location and location and listing_location != location:
            job.raw["listing_detail_location_reconciliation"] = {
                "listing_location": listing_location,
                "detail_location": location,
                "disposition": "Both public values are retained; the LinkedIn detail page supplies the separate detail value.",
            }
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        if identity not in self._published_ids:
            raise ValueError(f"Chargebee detail {identity} was not exposed by the current company list")
        if self._linkedin_429:
            job.description_unavailable = True
            job.description_method = "chargebee.linkedin.public_detail_deferred_after_http_429"
            job.description_source_url = job.url
            job.raw["detail_fetch_deferred"] = {
                "reason": "A prior exact public LinkedIn detail request returned HTTP 429; no further request was issued.",
                "first_limited_job_id": self._linkedin_429["job_id"],
            }
            job.warnings.append("detail_deferred_after_linkedin_429")
            self._rate_limit_deferred_ids.append(identity)
            return job
        cached = self.preloaded_detail_pages.get(identity)
        if cached and self._same_detail_path(job.url, cached["requested_url"], identity):
            self._reused_detail_ids.append(identity)
            self._archive_reused_detail(identity, cached)
            return self._detail_from_html(
                job, cached["body"].decode("utf-8", "replace"), cached["final_url"],
                {"sha256": cached["sha256"], "fetched_at": cached["fetched_at"], "reused_probe_response": True},
            )
        try:
            response = await self.http.get(job.url, headers=self.linkedin_headers)
        except FetchError as exc:
            if "HTTP 429" in str(exc):
                self._linkedin_429 = {"job_id": identity, "url": job.url, "error": str(exc)}
                self.result.metadata["linkedin_rate_limit_stop"] = dict(self._linkedin_429)
                # Preserve that this run could not obtain a public body. Keep
                # detail_fetch_failed so the generic resume cache never treats
                # a transient rate limit as a permanent empty publisher record.
                job.description_unavailable = True
                job.description_method = "chargebee.linkedin.job_detail.http_429"
                job.description_source_url = job.url
                job.raw["detail_fetch_failure"] = {
                    "status": 429,
                    "reason": "LinkedIn public detail request rate limited; no description body was returned.",
                }
            raise
        response_url = str(response.url)
        detail_meta = {
            "sha256": sha256(response.content).hexdigest(),
            "fetched_at": now(),
            "reused_probe_response": False,
        }
        return self._detail_from_html(job, response.text, response_url, detail_meta)

    async def _snapshot(self, phase: str):
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Chargebee LinkedIn global listing page budget exhausted")
        response = await self.http.get(self.linkedin_worldwide_url, headers=self.linkedin_headers)
        rows, total, pagination = _listing_rows(_soup(response.text), page_url=str(response.url))
        ids = [row["job_id"] for row in rows]
        stable_hashes = {row["job_id"]: _compact_hash(json.dumps(_stable_listing_row(row), ensure_ascii=False, sort_keys=True)) for row in rows}
        snapshot = {
            "phase": phase,
            "url": str(response.url),
            "total": total,
            "pages": 1,
            "page_size": len(rows),
            "page_num": "0",
            "ordered_ids": ids,
            "stable_row_sha256": stable_hashes,
            "url_by_id": {row["job_id"]: row["job_url"] for row in rows},
            "pagination_links": pagination,
            "full_html_sha256": sha256(response.content).hexdigest(),
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        self.result.timings[f"{phase}_listing_seconds"] = snapshot["duration_seconds"]
        return rows, total, snapshot

    async def run(self):
        global_preview, india_preview = await self._official_journey()

        initial_rows, total, initial_snapshot = await self._snapshot("initial")
        self.result.expected_total = total
        self.result.metadata["initial_listing_ids"] = initial_snapshot["ordered_ids"]
        self.result.metadata["company_published_job_ids"] = initial_snapshot["ordered_ids"]
        self.result.metadata["final_listing_ids"] = None
        initial_by_id = {row["job_id"]: row for row in initial_rows}
        if len(initial_rows) != total:
            self.result.errors.append("chargebee_initial_linkedin_company_listing_count_mismatch")
        if len({row["job_id"] for row in initial_rows}) != len(initial_rows):
            self.result.errors.append("chargebee_initial_linkedin_company_listing_duplicate_ids")
        india_ids = set(item["job_id"] for item in india_preview)
        global_profile_ids = set(item["job_id"] for item in global_preview)
        self.result.metadata["global_profile_preview_subset_verified"] = global_profile_ids.issubset(set(initial_by_id))
        self.result.metadata["india_preview_subset_verified"] = india_ids.issubset(set(initial_by_id))
        if not self.result.metadata["global_profile_preview_subset_verified"]:
            self.result.errors.append("chargebee_global_profile_preview_not_subset_of_worldwide_company_list")
        if not self.result.metadata["india_preview_subset_verified"]:
            self.result.errors.append("chargebee_india_profile_preview_not_subset_of_worldwide_company_list")
        self._published_ids = set(initial_by_id)

        reused_by_id = {}
        for identity, cached in self.preloaded_detail_pages.items():
            row = initial_by_id.get(str(identity))
            if row and self._same_detail_path(row["job_url"], cached["requested_url"], str(identity)):
                reused_by_id[str(identity)] = cached
        self.result.metadata["reused_detail_probe_ids"] = sorted(reused_by_id)
        self.preloaded_detail_pages = reused_by_id

        for row in initial_rows:
            job = self._make_job(row)
            self.add(job)
            cached = self.preloaded_detail_pages.get(str(job.job_id))
            if cached:
                hydrated = self._detail_from_html(
                    job, cached["body"].decode("utf-8", "replace"), cached["final_url"],
                    {"sha256": cached["sha256"], "fetched_at": cached["fetched_at"], "reused_probe_response": True},
                )
                hydrated.validate()
                hydrated.record_identity = job.key
                self.options.cached_jobs[job.url] = hydrated
                self._archive_reused_detail(str(job.job_id), cached)
                self._reused_detail_ids.append(str(job.job_id))
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)

        self.result.metadata["reused_detail_ids"] = sorted(set(self._reused_detail_ids))
        apply_urls = sorted({job.apply_url for job in self.result.jobs if job.apply_url})
        self.result.metadata["detail_apply_url_status"] = (
            f"Exact public hrefs captured for {len(apply_urls)} job(s)." if apply_urls else
            "Exact LinkedIn Apply buttons are present, but no public href is exposed; do not infer or submit a login-only destination."
        )
        self.result.metadata["job_requisition_id_status"] = "LinkedIn numeric posting IDs are the only exact exposed IDs; no separate requisition ID found"

        if self._linkedin_429:
            self.result.metadata["linkedin_rate_limit_deferred_job_ids"] = sorted(set(self._rate_limit_deferred_ids))
            self.result.metadata["final_listing_recheck"] = {
                "status": "not_attempted_after_linkedin_http_429",
                "reason": "Stop source requests after backend rate limiting; no final listing retry or bulk refresh was started.",
            }
            self.result.errors.append("chargebee_final_membership_recheck_skipped_after_linkedin_http_429")
            self.result.listing_complete = False
            return self.result

        final_rows, final_total, final_snapshot = await self._snapshot("final")
        final_by_id = {row["job_id"]: row for row in final_rows}
        self.result.metadata["initial_listing_ids"] = initial_snapshot["ordered_ids"]
        self.result.metadata["final_listing_ids"] = final_snapshot["ordered_ids"]
        self.result.metadata["final_job_url_by_id"] = final_snapshot["url_by_id"]
        if total != final_total or initial_snapshot["ordered_ids"] != final_snapshot["ordered_ids"]:
            self.result.errors.append("chargebee_initial_final_company_listing_membership_changed")
        if set(initial_by_id) != set(final_by_id):
            self.result.errors.append("chargebee_initial_final_company_listing_id_set_mismatch")
        for identity in set(initial_by_id) & set(final_by_id):
            if _stable_listing_row(initial_by_id[identity]) != _stable_listing_row(final_by_id[identity]):
                self.result.errors.append(f"chargebee_initial_final_public_listing_fields_changed:{identity}")
            job = next((item for item in self.result.jobs if item.job_id == identity), None)
            if job:
                job.raw["final_listing"] = final_by_id[identity]
                job.raw["listing_url_variants"] = {
                    "initial": initial_by_id[identity]["job_url"],
                    "final": final_by_id[identity]["job_url"],
                }
        self.result.listing_complete = not any(error.startswith("chargebee_") for error in self.result.errors)
        return self.result
