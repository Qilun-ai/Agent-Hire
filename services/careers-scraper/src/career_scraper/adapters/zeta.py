"""Zeta's own careers listings (US + India) and the exact Lever postings API they request.

Observed journey (see companies/zeta/journey-research.md):

- The ledger entry https://www.zeta.tech/us/careers/ is the live official
  careers page.  It is a WordPress page whose shipped bundle
  ``/us/wp-content/themes/beta_zeta/js/us-careers.js`` pins exactly one job
  source, ``https://api.lever.co/v0/postings/zeta?group=team&mode=json``, and
  renders every posting whose ``country != "IN"`` as a card linking to
  ``/us/careers/job-description/?<lever-posting-id>``.  There is no pagination:
  the unfiltered group response is rendered in one pass and the department /
  search controls only hide rows client-side.
- Zeta also publishes an India listing on its own site,
  https://www.zeta.tech/in/careers/work-with-us/ (the "Join Us" page),
  whose shipped bundle ``/in/wp-content/themes/zeta_india/js/work-with-us.js``
  requests the same single ``zeta`` Lever account and renders every posting
  whose ``country == "IN"`` as a card linking to
  ``/in/careers/job-description/?<lever-posting-id>``.
- The two company-published listings partition the account's 28 public
  postings (5 non-IN + 23 IN) exactly.  Neither page has pagination; filters
  (department tabs, keyword search; India also a location dropdown) are
  client-side only.  Membership is therefore the unfiltered response of the
  exact request shipped by both listings, split by the filters those listings
  themselves apply.
- Each detail route is a company template that fetches the same group
  response and renders the matching posting's ``description``, every
  ``lists[]`` section as ``<h2>`` + ``<ul>``, and ``additional`` (the
  per-posting equal-opportunity paragraph) inside the job body.  The adapter
  rebuilds that exact body and independently re-reads every posting from
  ``/v0/postings/zeta/<id>`` to prove the fields are identical per job.
- Lever exposes only an internal ``createdAt`` creation timestamp here; it is
  retained in ``raw`` but is not turned into an advertised posting date.
  No salary, requisition, updated-at or expiry field is published.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Job, text_html


OFFICIAL_ENTRY = "https://www.zeta.tech/us/careers/"
INDIA_LISTING = "https://www.zeta.tech/in/careers/work-with-us/"
LEVER_ACCOUNT = "zeta"
LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/zeta?group=team&mode=json"
LEVER_DETAIL_TEMPLATE = "https://api.lever.co/v0/postings/zeta/{job_id}?mode=json"
LEVER_HOSTED_TEMPLATE = "https://jobs.lever.co/zeta/{job_id}"
US_DETAIL_TEMPLATE = "https://www.zeta.tech/us/careers/job-description/?{job_id}"
INDIA_DETAIL_TEMPLATE = "https://www.zeta.tech/in/careers/job-description/?{job_id}"
EMPLOYER = "Zeta"
DESCRIPTION_METHOD = "zeta.company_detail_template.lever_posting_fields"
VERIFICATION_REVISION = "zeta.localized-listings-lever.1"

US_BUNDLE_PATH = "/us/wp-content/themes/beta_zeta/js/us-careers.js"
INDIA_BUNDLE_PATH = "/in/wp-content/themes/zeta_india/js/work-with-us.js"
US_DETAIL_BUNDLE_PATH = "/us/wp-content/themes/beta_zeta/js/job-description.js"
INDIA_DETAIL_BUNDLE_PATH = "/in/wp-content/themes/zeta_india/js/job-description.js"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LEVER_URL_RE = re.compile(r"https://api\.lever\.co/v0/postings/[A-Za-z0-9_.-]+[^'\"\s;]*")
NON_INDIA_FILTER_RE = re.compile(r"\.country\s*!=\s*[\"']IN[\"']")
INDIA_FILTER_RE = re.compile(r"\.country\s*==\s*[\"']IN[\"']")
PAGINATION_HINT_RE = re.compile(r"(?:\blimit=|\bskip=|\boffset=|load[ _-]?more|next[ _-]?page)", re.I)
RESPONSIBILITY_RE = re.compile(r"responsibilit", re.I)
QUALIFICATION_RE = re.compile(
    r"qualif|skill|experience|desired|requirement|what you(?:'|’)ll need|who you are|education", re.I
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def compact_body_hash(value: str | None) -> str:
    """Whitespace-insensitive body hash, comparable with the rendered DOM text."""
    import unicodedata

    compact = re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))
    return sha256(compact.encode("utf-8")).hexdigest()


def _bundle_src(soup: BeautifulSoup, response_url: str, expected_path: str) -> str | None:
    for script in soup.select("script[src]"):
        target = urljoin(response_url, script.get("src", ""))
        if urlsplit(target).path == expected_path:
            return target
    return None


def _filter_controls(soup: BeautifulSoup, *, locale: str) -> dict:
    return {
        "department_tabs_container": bool(soup.select_one("ul.departments-list")),
        "keyword_search_input": bool(soup.select_one("#searchRole")),
        "location_dropdown": bool(soup.select_one("ul.location-list")) and bool(soup.select_one(".location-name")),
        "listing_container": bool(soup.select_one("div.departments-content")),
        "pagination_controls": [
            node.get_text(" ", strip=True)
            for node in soup.select("button, a")
            if re.search(r"\b(?:next|previous|load more|show more|page)\b", node.get_text(" ", strip=True), re.I)
        ],
        "filters_applied_by_source": locale,
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.zeta.tech"
            or parsed.path.rstrip("/") != "/us/careers"):
        raise ValueError("Zeta US careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Zeta Careers":
        raise ValueError(f"Zeta US careers page title changed: {title!r}")
    if not soup.select_one("h1.designation") and not soup.select_one("#spinner"):
        raise ValueError("Zeta US careers page no longer ships the job-listing shell")
    bundle = _bundle_src(soup, response_url, US_BUNDLE_PATH)
    if bundle is None:
        raise ValueError("Zeta US careers page no longer loads its us-careers.js listing bundle")
    controls = _filter_controls(soup, locale='country != "IN"')
    if not controls["department_tabs_container"] or not controls["keyword_search_input"]:
        raise ValueError("Zeta US careers listing filter controls changed")
    return {"url": response_url, "title": title, "bundle_url": bundle, **controls}


def parse_india_listing(html: str, response_url: str = INDIA_LISTING) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.zeta.tech"
            or parsed.path.rstrip("/") != "/in/careers/work-with-us"):
        raise ValueError("Zeta India careers listing redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Zeta Careers - Join us":
        raise ValueError(f"Zeta India careers listing title changed: {title!r}")
    bundle = _bundle_src(soup, response_url, INDIA_BUNDLE_PATH)
    if bundle is None:
        raise ValueError("Zeta India careers listing no longer loads its work-with-us.js bundle")
    controls = _filter_controls(soup, locale='country == "IN"')
    if not controls["department_tabs_container"] or not controls["keyword_search_input"]:
        raise ValueError("Zeta India careers listing filter controls changed")
    return {"url": response_url, "title": title, "bundle_url": bundle, **controls}


def parse_listing_bundle(js: str, *, locale: str) -> dict:
    """Validate the shipped listing code pins one company-scoped Lever request."""
    if locale not in {"us", "india"}:
        raise ValueError(f"Unknown Zeta listing locale: {locale!r}")
    urls = sorted(set(LEVER_URL_RE.findall(js)))
    if urls != [LEVER_POSTINGS_URL]:
        raise ValueError(f"Zeta {locale} listing bundle changed its Lever request: {urls}")
    if "group=team" not in js or "mode=json" not in js:
        raise ValueError(f"Zeta {locale} listing bundle no longer requests grouped JSON postings")
    detail_prefix = "/us/careers/job-description/?" if locale == "us" else "/in/careers/job-description/?"
    if detail_prefix not in js:
        raise ValueError(f"Zeta {locale} listing bundle no longer links the {locale} detail route")
    if locale == "us":
        if not NON_INDIA_FILTER_RE.search(js) or INDIA_FILTER_RE.search(js):
            raise ValueError("Zeta US listing bundle no longer renders exactly the non-India postings")
    else:
        if not INDIA_FILTER_RE.search(js) or NON_INDIA_FILTER_RE.search(js):
            raise ValueError("Zeta India listing bundle no longer renders exactly the India postings")
    if PAGINATION_HINT_RE.search(js):
        raise ValueError(f"Zeta {locale} listing bundle now contains pagination hints; the UI must be re-inspected")
        # The inspected UI renders every matching row in one response; a new
        # pagination control would change membership semantics and must be
        # reviewed before this adapter is trusted again.
    return {
        "locale": locale,
        "lever_request_url": LEVER_POSTINGS_URL,
        "detail_link_prefix": detail_prefix,
        "filters_applied_by_source": 'country != "IN"' if locale == "us" else 'country == "IN"',
        "pagination": False,
        "renders_all_rows_in_one_response": True,
    }


def parse_detail_shell(html: str, response_url: str, *, locale: str, job_id: str) -> dict:
    """Validate an exact company detail route renders from the same Lever request."""
    if locale not in {"us", "india"}:
        raise ValueError(f"Unknown Zeta detail locale: {locale!r}")
    parts = urlsplit(response_url)
    expected_path = "/us/careers/job-description/" if locale == "us" else "/in/careers/job-description"
    if (parts.scheme != "https" or parts.netloc != "www.zeta.tech"
            or parts.path.rstrip("/") != expected_path.rstrip("/")
            or parts.query != job_id):
        raise ValueError(f"Zeta {locale} detail route redirected outside its inspected job URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    expected_title = "Zeta Careers - Job Description" if locale == "us" else "Job Description"
    if title != expected_title:
        raise ValueError(f"Zeta {locale} detail page title changed: {title!r}")
    bundle_path = US_DETAIL_BUNDLE_PATH if locale == "us" else INDIA_DETAIL_BUNDLE_PATH
    bundle = _bundle_src(soup, response_url, bundle_path)
    if bundle is None:
        raise ValueError(f"Zeta {locale} detail page no longer loads its job-description.js bundle")
    if not soup.select_one(".jobs-description__wrapper") or not soup.select_one(".list-details"):
        raise ValueError(f"Zeta {locale} detail page no longer ships the job-description body containers")
    return {
        "url": response_url,
        "title": title,
        "bundle_url": bundle,
        "description_container": ".description + .list-details",
        "apply_control": bool(soup.select_one("a.apply")),
    }


def _posting_row(row: dict) -> dict:
    identity = str(row.get("id") or "")
    issues = []
    if not UUID_RE.fullmatch(identity):
        issues.append("missing_uuid_id")
    if not normalized_title(row.get("text")):
        issues.append("missing_title")
    if not isinstance(row.get("country"), str) or not row["country"].strip():
        issues.append("missing_country")
    categories = row.get("categories")
    if not isinstance(categories, dict):
        issues.append("missing_categories")
        categories = {}
    for field in ("location", "department", "commitment", "team"):
        if not isinstance(categories.get(field), str) or not categories[field].strip():
            issues.append(f"missing_category_{field}")
    if not isinstance(row.get("description"), str) or not row["description"].strip():
        issues.append("missing_description")
    if not isinstance(row.get("additional"), str):
        issues.append("missing_additional_field")
    lists = row.get("lists")
    if not isinstance(lists, list) or not lists:
        issues.append("missing_lists")
    else:
        for entry in lists:
            if (not isinstance(entry, dict) or not isinstance(entry.get("text"), str)
                    or not entry["text"].strip() or not isinstance(entry.get("content"), str)):
                issues.append("malformed_list_section")
                break
    hosted = row.get("hostedUrl")
    apply_url = row.get("applyUrl")
    if hosted != LEVER_HOSTED_TEMPLATE.format(job_id=identity):
        issues.append("hosted_url_not_account_scoped")
    if apply_url != f"{LEVER_HOSTED_TEMPLATE.format(job_id=identity)}/apply":
        issues.append("apply_url_not_account_scoped")
    if not isinstance(row.get("workplaceType"), str) or not row["workplaceType"].strip():
        issues.append("missing_workplace_type")
    return {"id": identity, "issues": issues}


def parse_lever_listing(value) -> dict:
    if not isinstance(value, list) or not value:
        raise ValueError("Zeta Lever postings response is not a non-empty team list")
    teams, rows, issues, seen = [], [], [], set()
    for team in value:
        if not isinstance(team, dict) or not isinstance(team.get("postings"), list):
            issues.append("team_without_postings")
            continue
        team_title = normalized_title(team.get("title"))
        if not team_title:
            issues.append("team_without_title")
        teams.append({"title": team_title, "postings": len(team["postings"])})
        for posting in team["postings"]:
            if not isinstance(posting, dict):
                issues.append("posting_not_object")
                continue
            checked = _posting_row(posting)
            if checked["issues"]:
                issues.extend(f"{issue}:{checked['id'] or 'unknown'}" for issue in checked["issues"])
                continue
            identity = checked["id"]
            if identity in seen:
                issues.append(f"duplicate_posting_id:{identity}")
                continue
            seen.add(identity)
            rows.append(posting)
    if not rows:
        raise ValueError("Zeta Lever postings response contains no usable posting rows")
    return {
        "teams": teams,
        "team_count": len(teams),
        "rows": rows,
        "count": len(rows),
        "job_ids": [str(row["id"]) for row in rows],
        "issues": list(dict.fromkeys(issues)),
    }


def partition_membership(rows: list[dict]) -> dict:
    """Split the exact group response by the filters each company listing applies."""
    us_rows = [row for row in rows if row.get("country") != "IN"]
    india_rows = [row for row in rows if row.get("country") == "IN"]
    ids = [str(row["id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Zeta Lever response repeats a posting id")
    if len(us_rows) + len(india_rows) != len(rows):
        raise ValueError("Zeta country partition lost a posting")
    return {
        "us_listing_rows": us_rows,
        "india_listing_rows": india_rows,
        "us_count": len(us_rows),
        "india_count": len(india_rows),
        "total": len(rows),
        "complete": len(rows) == len(ids),
    }


def detail_route(row: dict) -> str:
    identity = str(row["id"])
    if not UUID_RE.fullmatch(identity):
        raise ValueError(f"Zeta posting id is not a Lever UUID: {identity!r}")
    template = INDIA_DETAIL_TEMPLATE if row.get("country") == "IN" else US_DETAIL_TEMPLATE
    return template.format(job_id=identity)


def membership_locale(row: dict) -> str:
    return "india" if row.get("country") == "IN" else "us"


def compose_description(row: dict) -> str:
    """Rebuild the company detail body: description, each list, then additional."""
    parts = [row.get("description") or ""]
    for section in row.get("lists") or []:
        parts.append(f"<h2>{section.get('text') or ''}</h2><ul>{section.get('content') or ''}</ul>")
    parts.append(row.get("additional") or "")
    return "".join(parts)


def description_sections(row: dict) -> list[dict]:
    sections = []
    for entry in row.get("lists") or []:
        heading = normalized_title(entry.get("text"))
        html = entry.get("content") or ""
        sections.append({"heading": heading, "html": html, "text": text_html(html)})
    return sections


def section_roles(sections: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    responsibilities, qualifications, other = [], [], []
    for section in sections:
        if RESPONSIBILITY_RE.search(section["heading"] or ""):
            responsibilities.append(section)
        elif QUALIFICATION_RE.search(section["heading"] or ""):
            qualifications.append(section)
        else:
            other.append(section)
    return responsibilities, qualifications, other


def parse_lever_detail(value, job_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Zeta per-posting Lever response is not an object")
    if str(value.get("id")) != str(job_id):
        raise ValueError(f"Zeta per-posting Lever id {value.get('id')!r} != requested {job_id!r}")
    checked = _posting_row(value)
    if checked["issues"]:
        raise ValueError(f"Zeta per-posting Lever row changed: {checked['issues']}")
    return value


class Zeta(Adapter):
    """Read only Zeta's own US/India careers listings and their exact Lever feed."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    def _record(self, response, started: float) -> dict:
        return {
            "status": response.status_code,
            "final_url": str(response.url),
            "url": str(response.request.url),
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started), 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type"),
            "redirects": [str(item.url) for item in response.history],
        }

    async def _listing(self):
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Zeta page budget exhausted before the US listing")
        entry_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry.update(self._record(entry_response, entry_started))

        if not self.page_guard():
            raise ValueError("Zeta page budget exhausted before the India listing")
        india_started = time.perf_counter()
        india_response = await self.http.get(INDIA_LISTING)
        india = parse_india_listing(india_response.text, str(india_response.url))
        india.update(self._record(india_response, india_started))

        bundles = {}
        for locale, url in (("us", entry["bundle_url"]), ("india", india["bundle_url"])):
            if not self.page_guard():
                raise ValueError(f"Zeta page budget exhausted before the {locale} listing bundle")
            bundle_started = time.perf_counter()
            response = await self.http.get(url)
            parsed = parse_listing_bundle(response.text, locale=locale)
            parsed.update(self._record(response, bundle_started))
            bundles[locale] = parsed
        return {
            "us": entry,
            "india": india,
            "bundles": bundles,
            "wall_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _postings(self):
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Zeta page budget exhausted before the Lever listing")
        response = await self.http.get(LEVER_POSTINGS_URL)
        listing = parse_lever_listing(response.json())
        listing.update(self._record(response, started))
        listing["request_seconds"] = listing["elapsed_seconds"]
        return listing

    def _make_job(self, row: dict, *, us_ids: list[str], india_ids: list[str]) -> Job:
        identity = str(row["id"])
        description_html = compose_description(row)
        sections = description_sections(row)
        responsibilities, qualifications, other = section_roles(sections)
        locations = row["categories"].get("allLocations") or [row["categories"]["location"]]
        locale = membership_locale(row)
        job = self.job(
            {
                "listing": dict(row),
                "membership": {
                    "locale": locale,
                    "listing_url": OFFICIAL_ENTRY if locale == "us" else INDIA_LISTING,
                    "position_in_locale_listing": (us_ids if locale == "us" else india_ids).index(identity) + 1,
                },
            },
            title=normalized_title(row["text"]),
            url=detail_route(row),
            job_id=identity,
            requisition_id=None,
            apply_url=row["applyUrl"],
            description=description_html,
            method=DESCRIPTION_METHOD,
            description_url=detail_route(row),
            locations=list(locations),
            department=row["categories"]["department"],
            team=row["categories"]["team"],
            employment_type=row["categories"]["commitment"],
            workplace_type=row["workplaceType"],
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=EMPLOYER,
            language="en",
            responsibilities=[section["text"] for section in responsibilities] or None,
            qualifications=[section["text"] for section in qualifications] or None,
        )
        job.description_text = text_html(description_html)
        job.company_scope_verified = self.company.scope == "company"
        job.raw.update({
            "source_locale": locale,
            "country": row.get("country"),
            "hosted_url": row.get("hostedUrl"),
            "additional_html": row.get("additional") or "",
            "additional_plain": row.get("additionalPlain") or "",
            "role_body_html": "".join(
                [row["description"]] + [f"<h2>{s.get('text') or ''}</h2><ul>{s.get('content') or ''}</ul>"
                                        for s in row.get("lists") or []]
            ),
            "sections": {"responsibilities": responsibilities, "qualifications": qualifications, "other": other},
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_compact_sha256": compact_body_hash(job.description_text),
            "description_normalized_chars": len(job.description_text),
            "dates": {
                "createdAt": row.get("createdAt"),
                "createdAt_semantics": "Lever internal posting creation timestamp; not used as a posting date",
                "date_posted_mapping": None,
                "date_updated": None,
                "valid_through": None,
            },
            "description_source_fields": ["description", "lists[].text/content", "additional"],
            "application_route": {
                "company_detail_url": detail_route(row),
                "lever_apply_url": row.get("applyUrl"),
                "lever_hosted_url": row.get("hostedUrl"),
            },
        })
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "india_listing_url": INDIA_LISTING,
            "lever_postings_url": LEVER_POSTINGS_URL,
            "lever_account": LEVER_ACCOUNT,
            "listing_authority": (
                "The two job listings published by Zeta's own site (US careers page and "
                "India 'Join Us' page), which both request the same zeta Lever account and "
                "partition it by the country filters shipped in their bundles."
            ),
            "detail_method": (
                "Company detail route template plus the per-posting zeta Lever object it "
                "renders; description, every lists[] section and the per-posting additional "
                "paragraph are retained verbatim."
            ),
            "request_scope": "Only the zeta Lever account and the exact company-published listing/detail routes.",
        })
        try:
            listing = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Zeta careers listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["listing_pages"] = {
            "us": {k: v for k, v in listing["us"].items()},
            "india": {k: v for k, v in listing["india"].items()},
            "bundles": listing["bundles"],
        }
        self.result.timings["listing_phase_seconds"] = listing["wall_seconds"]

        api_started = time.perf_counter()
        try:
            feed = await self._postings()
        except Exception as exc:
            self.result.errors.append(f"Zeta Lever postings request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.timings["api_phase_seconds"] = round(max(0.0, time.perf_counter() - api_started), 6)

        membership = partition_membership(feed["rows"])
        us_ids = [str(row["id"]) for row in membership["us_listing_rows"]]
        india_ids = [str(row["id"]) for row in membership["india_listing_rows"]]
        self.result.expected_total = membership["total"]
        if feed["issues"]:
            self.result.errors.extend(f"Zeta Lever row: {issue}" for issue in feed["issues"])
        self.result.metadata["lever_listing"] = {
            key: value for key, value in feed.items() if key != "rows"
        }
        self.result.metadata["membership"] = {
            "advertised_jobs": membership["total"],
            "us_listing_jobs": membership["us_count"],
            "india_listing_jobs": membership["india_count"],
            "us_listing_job_ids": us_ids,
            "india_listing_job_ids": india_ids,
            "partition_covers_every_posting": (
                len(set(us_ids)) + len(set(india_ids)) == membership["total"] == len(set(us_ids) | set(india_ids))
            ),
            "pagination": {"us": False, "india": False},
            "filters_applied": {"us": 'country != "IN"', "india": 'country == "IN"'},
        }
        if not self.result.metadata["membership"]["partition_covers_every_posting"]:
            self.result.errors.append("Zeta country partition no longer covers the Lever account")

        for row in feed["rows"]:
            self.add(self._make_job(row, us_ids=us_ids, india_ids=india_ids))
        if self.limit():
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        async def fetch(url, parse):
            attempts = 0
            while True:
                attempts += 1
                try:
                    response = await self.http.get(url)
                    return parse(response), response, attempts
                except Exception:
                    # Both exact routes were observed healthy. Transient resets
                    # on this host are retried on the same job only; a real
                    # failure still surfaces after the bounded attempts.
                    if attempts >= 3:
                        raise
                    await asyncio.sleep(1.5 * attempts)

        async def detail(job):
            detail_started = time.perf_counter()
            page_started = time.perf_counter()
            page, page_response, page_attempts = await fetch(
                job.url,
                lambda response: parse_detail_shell(
                    response.text, str(response.url),
                    locale=job.raw["source_locale"], job_id=job.job_id,
                ),
            )
            page.update(self._record(page_response, page_started))
            body_started = time.perf_counter()
            body, body_response, body_attempts = await fetch(
                LEVER_DETAIL_TEMPLATE.format(job_id=job.job_id),
                lambda response: parse_lever_detail(response.json(), job.job_id),
            )
            body_record = self._record(body_response, body_started)
            listing_row = job.raw["listing"]
            if body != listing_row:
                job.warnings.append("detail_posting_fields_differ_from_listing_row")
                self.result.errors.append(f"Zeta posting {job.job_id} detail fields differ from the listing row")
                job.description_html = compose_description(body)
                job.description_text = text_html(job.description_html)
                job.raw["listing"] = dict(body)
            job.raw["detail"] = {
                "page": page,
                "posting": body,
                "posting_response": body_record,
                "attempts": {"page": page_attempts, "posting": body_attempts},
                "fields_equal_to_listing_row": body == listing_row,
                "description_source_fields": ["description", "lists[].text/content", "additional"],
                "description_sha256": sha256(job.description_html.encode()).hexdigest(),
                "description_compact_sha256": compact_body_hash(job.description_text),
                "description_normalized_chars": len(job.description_text),
                "detail_phase_seconds": round(max(0.0, time.perf_counter() - detail_started), 6),
            }
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        self.result.metadata["hydration"] = {
            "jobs": len(self.result.jobs),
            "detail_page_requests": len(self.result.jobs),
            "per_posting_requests": len(self.result.jobs),
            "detail_route": {"us": US_DETAIL_TEMPLATE, "india": INDIA_DETAIL_TEMPLATE},
            "detail_endpoint": LEVER_DETAIL_TEMPLATE,
        }
        detail_pages = sum("page" in job.raw.get("detail", {}) for job in self.result.jobs)
        equal_rows = sum(job.raw.get("detail", {}).get("fields_equal_to_listing_row") is True
                         for job in self.result.jobs)
        collected_ids = [str(job.job_id) for job in self.result.jobs]
        self.result.metadata["reconciliation"] = {
            "advertised_jobs": membership["total"],
            "us_listing_rows_observed": membership["us_count"],
            "india_listing_rows_observed": membership["india_count"],
            "collected_jobs": len(self.result.jobs),
            "job_ids_match_api_order": collected_ids == feed["job_ids"],
            "detail_routes_checked": detail_pages,
            "per_posting_details_equal_listing_row": equal_rows,
        }
        self.result.listing_complete = (
            not self.result.errors
            and "sample_limit" not in self.result.warnings
            and len(self.result.jobs) == membership["total"]
            and collected_ids == feed["job_ids"]
            and detail_pages == len(self.result.jobs)
            and equal_rows == len(self.result.jobs)
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
