"""JetBrains' own careers listing and the company-scoped Greenhouse board it links.

JetBrains' official page https://www.jetbrains.com/careers/jobs/ renders its
global vacancy list from the company's own
``https://resources.jetbrains.com/storage/greenhouse/greenhouse-data.json``
feed.  The same page bundle links every card to
``https://job-boards.eu.greenhouse.io/jetbrains/jobs/<id>``; those exact
company-board pages carry the full role body and the application form.  The
careers page paginates the 32 feed rows client-side (8 cards per page,
Locations/Job Category/Team filters).  Membership is therefore the feed's
``meta.total`` and its rows, not a guessed ATS slug or a board-wide query.

Only the exact ``.job__description`` container is treated as the role body;
the embedded application questions, the page footer and the logo are not.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html
from ..structured import embedded_page_object


OFFICIAL_ENTRY = "https://www.jetbrains.com/careers/jobs/"
LISTING_FEED_URL = "https://resources.jetbrains.com/storage/greenhouse/greenhouse-data.json"
BOARD_ORIGIN = "https://job-boards.eu.greenhouse.io"
BOARD_PATH = "/jetbrains"
BOARD_URL = BOARD_ORIGIN + BOARD_PATH
JOB_PATH = re.compile(r"^/jetbrains/jobs/(\d+)$")
EMBEDDED_ROUTE = "routes/careers/jobs/page"
FEED_KEY = "greenhouse-data.json"
EMPLOYER = "JetBrains"
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace-normalised visible text used by the independent HTML audit."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _iso_date(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"JetBrains {field} is not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"JetBrains {field} is not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"JetBrains {field} has no timezone: {value!r}")
    return parsed.date().isoformat()


def _metadata_value(row: dict, name: str):
    for item in row.get("metadata") or []:
        if isinstance(item, dict) and normalized_title(item.get("name")).casefold() == name.casefold():
            return item.get("value")
    return None


def parse_entry_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.jetbrains.com"
            or parsed.path.rstrip("/") != "/careers/jobs"):
        raise ValueError("JetBrains official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Open Positions - JetBrains":
        raise ValueError(f"JetBrains official entry title changed: {title!r}")
    if soup.select_one(".vacancy-list-page") is None:
        raise ValueError("JetBrains official entry no longer renders the vacancy-list page")

    module_text = None
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        value = script.get_text()
        if "__reactRouterRouteModules" in value:
            module_text = value
            break
    if module_text is None:
        raise ValueError("JetBrains official entry no longer publishes its React Router route modules")
    imports = dict(re.findall(r'import \* as (route\d+) from "([^"]+)"', module_text))
    match = re.search(r'"' + re.escape(EMBEDDED_ROUTE) + r'":\s*(route\d+)', module_text)
    if match is None or match.group(1) not in imports:
        raise ValueError("JetBrains official entry no longer maps the careers/jobs route module")
    module_url = urljoin(response_url, imports[match.group(1)])
    module_parts = urlsplit(module_url)
    if (module_parts.scheme != "https" or module_parts.netloc != "www.jetbrains.com"
            or not module_parts.path.startswith("/_assets/rr/page-")):
        raise ValueError("JetBrains jobs route module is not the inspected first-party page bundle")
    return {
        "url": response_url,
        "title": title,
        "page_module_url": module_url,
        "listing_feed_key": FEED_KEY,
        "embedding": "official careers/jobs page itself requests greenhouse-data.json and links the company board",
    }


def parse_page_module(js: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.jetbrains.com"
            or not parsed.path.startswith("/_assets/rr/page-")):
        raise ValueError("JetBrains jobs page module came from an unexpected URL")
    if FEED_KEY not in js:
        raise ValueError("JetBrains jobs page module no longer requests greenhouse-data.json")
    if BOARD_ORIGIN + BOARD_PATH not in js:
        raise ValueError("JetBrains jobs page module no longer links the /jetbrains board")
    if "job-boards.eu.greenhouse.io/jetbrains/jobs/${" not in js:
        raise ValueError("JetBrains jobs page module no longer builds exact /jetbrains/jobs/<id> links")
    return {
        "url": response_url,
        "sha256": hashlib.sha256(js.encode()).hexdigest(),
        "bytes": len(js.encode()),
        "contains_listing_feed_request": True,
        "contains_company_board_origin": True,
        "contains_exact_job_route_template": True,
    }


def parse_listing_feed(payload, response_url: str = LISTING_FEED_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "resources.jetbrains.com"
            or parsed.path != "/storage/greenhouse/greenhouse-data.json"):
        raise ValueError("JetBrains careers feed came from an unexpected URL")
    if not isinstance(payload, dict):
        raise ValueError("JetBrains careers feed is not a JSON object")
    meta = payload.get("meta")
    rows = payload.get("jobs")
    if not isinstance(meta, dict) or not isinstance(meta.get("total"), int) or meta["total"] < 0:
        raise ValueError("JetBrains careers feed has no integer meta.total")
    if not isinstance(rows, list):
        raise ValueError("JetBrains careers feed has no jobs array")
    if len(rows) != meta["total"]:
        raise ValueError(
            f"JetBrains careers feed total {meta['total']} does not reconcile with {len(rows)} rows"
        )
    ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"JetBrains careers feed row {index} is not an object")
        identity = row.get("id")
        if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
            raise ValueError(f"JetBrains careers feed row {index} has no positive integer id")
        if not normalized_title(row.get("title")):
            raise ValueError(f"JetBrains careers feed row {index} has no title")
        location = row.get("location")
        if not isinstance(location, dict) or not isinstance(location.get("name"), str) or not location["name"].strip():
            raise ValueError(f"JetBrains careers feed row {index} has no location name")
        if not isinstance(row.get("language"), str) or not row["language"].strip():
            raise ValueError(f"JetBrains careers feed row {index} has no language")
        for field in ("departments", "offices", "metadata"):
            values = row.get(field)
            if not isinstance(values, list):
                raise ValueError(f"JetBrains careers feed row {index} has no {field} array")
            for item in values:
                if not isinstance(item, dict) or not normalized_title(item.get("name")):
                    raise ValueError(f"JetBrains careers feed row {index} has an invalid {field} entry")
        identity = str(identity)
        if identity in ids:
            raise ValueError(f"JetBrains careers feed repeats job id {identity}")
        ids.append(identity)
    return {
        "url": response_url,
        "total": meta["total"],
        "jobs": rows,
        "ids": ids,
        "meta": meta,
        "field_names": sorted({key for row in rows for key in row}),
    }


def _job_url(identity) -> str:
    return f"{BOARD_ORIGIN}{BOARD_PATH}/jobs/{identity}"


def _attachment_links(html: str) -> list[dict]:
    links = []
    for anchor in BeautifulSoup(html or "", "lxml").select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT_SUFFIX.search(href):
            links.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return links


def parse_job_detail(html: str, listing_row: dict, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parsed.path)
    identity = str(listing_row.get("id"))
    if parsed.scheme != "https" or parsed.netloc != "job-boards.eu.greenhouse.io" or match is None:
        raise ValueError("JetBrains job page redirected away from its exact company board URL")
    if match.group(1) != identity:
        raise ValueError("JetBrains job page id does not match the listed id")

    soup = BeautifulSoup(html, "lxml")
    containers = soup.select(".job__description")
    if len(containers) != 1:
        raise ValueError("JetBrains job page does not expose one .job__description container")
    description_html = "".join(str(child) for child in containers[0].contents).strip()
    description_text = text_html(description_html)
    if not description_text:
        raise ValueError("JetBrains .job__description container is empty")

    context = embedded_page_object(html, "window.__remixContext = ")
    loader_data = context.get("state", {}).get("loaderData") if isinstance(context, dict) else None
    if not isinstance(loader_data, dict):
        raise ValueError("JetBrains job page has no loader data")
    job_posts = [
        value["jobPost"] for value in loader_data.values()
        if isinstance(value, dict) and isinstance(value.get("jobPost"), dict)
    ]
    if len(job_posts) != 1:
        raise ValueError("JetBrains job page does not publish exactly one jobPost object")
    job_post = job_posts[0]

    if job_post.get("post_type") != "job_post":
        raise ValueError("JetBrains page is not a public job_post")
    if normalized_title(job_post.get("title")) != normalized_title(listing_row.get("title")):
        raise ValueError(
            f"JetBrains title conflict listing={listing_row.get('title')!r} detail={job_post.get('title')!r}"
        )
    public_url = job_post.get("public_url")
    if not isinstance(public_url, str):
        raise ValueError("JetBrains jobPost has no public_url")
    public_parts = urlsplit(public_url)
    if public_parts.scheme != "https" or public_parts.netloc != urlsplit(BOARD_ORIGIN).netloc or public_parts.path != parsed.path:
        raise ValueError("JetBrains jobPost public_url does not match the fetched company board page")
    if str(job_post.get("company_name", "")).casefold() != EMPLOYER.casefold():
        raise ValueError("JetBrains jobPost company_name is not JetBrains")
    if job_post.get("language") != "en":
        raise ValueError("JetBrains jobPost language is not en")
    listing_location = listing_row.get("location", {}).get("name")
    if job_post.get("job_post_location") != listing_location:
        raise ValueError(
            f"JetBrains location conflict listing={listing_location!r} detail={job_post.get('job_post_location')!r}"
        )
    content = job_post.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("JetBrains jobPost content is missing")
    introduction = job_post.get("introduction") or ""
    conclusion = job_post.get("conclusion") or ""
    if not isinstance(introduction, str) or not isinstance(conclusion, str):
        raise ValueError("JetBrains jobPost introduction/conclusion has an invalid type")
    content_text = text_html(introduction + content)
    conclusion_text = text_html(conclusion)
    if not content_text or not description_text.startswith(content_text):
        raise ValueError("JetBrains visible description does not begin with the published jobPost content")
    if conclusion_text and not description_text.endswith(conclusion_text):
        raise ValueError("JetBrains visible description does not end with the published jobPost conclusion")

    pay_ranges = job_post.get("pay_ranges") or []
    if not isinstance(pay_ranges, list):
        raise ValueError("JetBrains jobPost pay_ranges is not an array")
    salary = None
    if pay_ranges:
        salary = []
        for item in pay_ranges:
            if not isinstance(item, dict) or not item.get("min") or not item.get("max"):
                raise ValueError("JetBrains jobPost pay range is missing min/max")
            for value in (item.get("min"), item.get("max")):
                if str(value) not in description_text:
                    raise ValueError("JetBrains visible description does not contain its published pay range")
            salary.append({
                "title": item.get("title"),
                "description": item.get("description"),
                "min": item.get("min"),
                "max": item.get("max"),
                "currency_type": item.get("currency_type"),
                "source_field": "jobPost.pay_ranges",
            })

    published_at = job_post.get("published_at")
    date_posted = _iso_date(published_at, "published_at")
    valid_through = _iso_date(job_post.get("application_deadline"), "application_deadline")
    employment = job_post.get("employment")
    employment_type = None
    if isinstance(employment, str) and employment.strip().casefold() not in {"", "hidden"}:
        employment_type = employment.strip()
    parts = [part.strip() for part in listing_location.split(";") if part.strip()]
    remote = any(re.match(r"^remote(?:\b|,)", part, re.I) for part in parts)
    departments = [normalized_title(item.get("name")) for item in listing_row.get("departments") or []]
    job_category = _metadata_value(listing_row, "Job Category")
    team = _metadata_value(listing_row, "Team")

    form = soup.find("form", id="application-form")
    form_action = form.get("action") if form else None
    form_method = (form.get("method") or "").lower() if form else None
    if form is not None and form_action not in (f"{BOARD_PATH}/jobs/{identity}", response_url):
        raise ValueError("JetBrains application form action does not stay on the exact job page")
    headings = [
        normalized_title(node.get_text(" ", strip=True))
        for node in containers[0].find_all(["h1", "h2", "h3"])
        if normalized_title(node.get_text(" ", strip=True))
    ]
    tags_container = soup.select_one(".job__tags")
    tags = normalized_title(tags_container.get_text(" ", strip=True)) if tags_container else ""
    return {
        "id": identity,
        "title": normalized_title(job_post.get("title")),
        "description_html": description_html,
        "description_text": description_text,
        "date_posted": date_posted,
        "date_posted_raw": published_at,
        "valid_through": valid_through,
        "salary": salary,
        "locations": [{"location": part} for part in parts],
        "department": departments,
        "team": team,
        "job_category": job_category,
        "employment_type": employment_type,
        "employment_raw": employment,
        "workplace_type": "Remote" if remote else None,
        "employer": job_post.get("company_name"),
        "language": job_post.get("language"),
        "apply_url": public_url,
        "job_post": job_post,
        "job_post_field_names": sorted(job_post),
        "source_ui": {
            "detail_url": response_url,
            "public_url": public_url,
            "board_url": BOARD_URL,
            "description_container": ".job__description",
            "description_visible_chars": len(description_text),
            "description_normalized_chars": len(normalized_body_text(description_html)),
            "description_normalized_fnv1a64": fnv1a64(normalized_body_text(description_html)),
            "description_sha256": hashlib.sha256(description_html.encode()).hexdigest(),
            "description_fnv1a64": fnv1a64(description_text),
            "employment_raw": employment,
            "description_section_headings": headings,
            "pay_range_count": len(pay_ranges),
            "tags_text": tags,
            "application_form": {
                "id": form.get("id") if form else None,
                "action": form_action,
                "method": form_method,
                "apply_button_text": next(
                    (normalized_title(button.get_text(" ", strip=True)) for button in soup.find_all("button")
                     if button.get("aria-label") == "Apply"),
                    None,
                ),
            },
            "attachment_links": _attachment_links(description_html),
            "listing_title_raw": listing_row.get("title"),
            "listing_location_raw": listing_location,
            "listing_office_names": [item.get("name") for item in listing_row.get("offices") or []],
            "job_category": job_category,
            "team": team,
        },
    }


class JetBrains(Adapter):
    """Read JetBrains' own careers feed and its exact linked board details."""

    VERIFICATION_REVISION = "jetbrains.official-careers-feed-and-company-board-details.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_parse_seconds = 0.0
        self.entry = {}

    async def detail(self, job):
        response = await self.http.get(job.url)
        started = time.perf_counter()
        parsed = parse_job_detail(response.text, job.raw["listing"], str(response.url))
        self.detail_parse_seconds += max(0.0, time.perf_counter() - started)
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "jetbrains.careers_greenhouse_feed+board.job__description"
        job.description_source_url = str(response.url)
        job.apply_url = parsed["apply_url"]
        job.locations = parsed["locations"]
        job.department = parsed["department"]
        job.team = parsed["team"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.date_updated = None
        job.valid_through = parsed["valid_through"]
        job.salary = parsed["salary"]
        job.employer = parsed["employer"]
        job.language = parsed["language"]
        job.raw["detail"] = {
            "job_post": parsed["job_post"],
            "job_post_field_names": parsed["job_post_field_names"],
            "response_url": str(response.url),
            "status": response.status_code,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        job.raw["source_ui"] = parsed["source_ui"]
        job.raw["native_detail_url"] = str(response.url)
        return job

    async def run(self):
        if not self.page_guard():
            return self.result

        source_started = time.perf_counter()
        entry_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        self.entry = parse_entry_page(entry.text, str(entry.url))
        entry_seconds = max(0.0, time.perf_counter() - entry_started)

        module_started = time.perf_counter()
        module = await self.http.get(self.entry["page_module_url"])
        module_info = parse_page_module(module.text, str(module.url))
        module_seconds = max(0.0, time.perf_counter() - module_started)

        listing_started = time.perf_counter()
        feed = await self.http.get(LISTING_FEED_URL)
        try:
            payload = feed.json()
        except ValueError as exc:
            raise ValueError("JetBrains careers feed is not JSON") from exc
        listing = parse_listing_feed(payload, str(feed.url))
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        source_collection_seconds = max(0.0, time.perf_counter() - source_started)

        self.result.metadata["official_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "entry_final_url": str(entry.url),
            "entry_status": entry.status_code,
            "entry_title": self.entry["title"],
            "page_module_url": self.entry["page_module_url"],
            "page_module": module_info,
            "listing_feed_url": LISTING_FEED_URL,
            "listing_feed_status": feed.status_code,
            "company_board_url": BOARD_URL,
            "scope_reason": (
                "JetBrains' own careers/jobs page requests its greenhouse-data.json feed and "
                "builds every card link as https://job-boards.eu.greenhouse.io/jetbrains/jobs/<id>."
            ),
        }
        self.result.metadata["source_fields"] = {
            "listing_feed_fields": listing["field_names"],
            "listing_total": listing["total"],
            "detail_job_post_fields": [],
            "description_container": ".job__description",
            "date_semantics": (
                "jobPost.published_at is the board's public publication timestamp; "
                "date_posted is its ISO date. No date_updated field exists; "
                "application_deadline is retained as valid_through when present."
            ),
            "salary_semantics": "jobPost.pay_ranges is retained verbatim and also stays inside the visible description.",
            "apply_semantics": (
                "The Apply button opens the embedded Greenhouse application form on the same exact "
                "job page; apply_url is that exact page."
            ),
        }
        self.result.timings.update({
            "entry_request_seconds": entry_seconds,
            "page_module_request_seconds": module_seconds,
            "listing_feed_request_seconds": listing_seconds,
            "source_collection_seconds": source_collection_seconds,
        })

        for row in listing["jobs"]:
            identity = str(row["id"])
            self.add(self.job(
                {"listing": row},
                title=normalized_title(row.get("title")),
                job_id=identity,
                url=_job_url(identity),
                date_posted=None,
                apply_url=None,
            ))
        self.result.expected_total = listing["total"]

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["detail_phase_wall_seconds"] = detail_seconds
        self.result.timings["detail_parse_seconds"] = self.detail_parse_seconds
        self.result.metadata["source_fields"]["detail_job_post_fields"] = sorted({
            key for job in self.result.jobs for key in job.raw.get("detail", {}).get("job_post_field_names", [])
        })

        final_started = time.perf_counter()
        final_entry = await self.http.get(OFFICIAL_ENTRY)
        final_entry_info = parse_entry_page(final_entry.text, str(final_entry.url))
        final_feed = await self.http.get(LISTING_FEED_URL)
        final_listing = parse_listing_feed(final_feed.json(), str(final_feed.url))
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_recheck_seconds"] = final_seconds

        initial_ids = listing["ids"]
        final_ids = final_listing["ids"]
        collected_ids = [job.job_id for job in self.result.jobs]
        initial_rows = {str(row["id"]): row for row in listing["jobs"]}
        final_rows = {str(row["id"]): row for row in final_listing["jobs"]}
        changed = {}
        for identity in sorted(set(initial_rows) | set(final_rows)):
            before, after = initial_rows.get(identity), final_rows.get(identity)
            before_view = None if before is None else (
                normalized_title(before.get("title")), before.get("location", {}).get("name")
            )
            after_view = None if after is None else (
                normalized_title(after.get("title")), after.get("location", {}).get("name")
            )
            if before_view != after_view:
                changed[identity] = {"initial": before_view, "final": after_view}
        entry_module_changed = final_entry_info["page_module_url"] != self.entry["page_module_url"]
        order_matches = initial_ids == final_ids == collected_ids
        reconciled = (
            listing["total"] == final_listing["total"] == len(self.result.jobs)
            and order_matches and not changed and not entry_module_changed
        )
        if not reconciled:
            self.result.errors.append("jetbrains_membership_reconciliation_failed")
        self.result.metadata["membership_reconciliation"] = {
            "initial_total": listing["total"],
            "final_total": final_listing["total"],
            "jobs_collected": len(self.result.jobs),
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "collected_ids": collected_ids,
            "initial_final_order_matches": initial_ids == final_ids,
            "initial_collected_order_matches": initial_ids == collected_ids,
            "changed_rows": changed,
            "entry_page_module_changed": entry_module_changed,
            "complete": reconciled,
            "pagination": "Official page paginates the same 32-row feed client-side at 8 cards per page (4 pages), with Locations/Job Category/Team filters; no additional feed page.",
            "filters_observed": ["Locations", "Job Category", "Team", "search"],
        }
        self.result.metadata["details"] = self.result.metadata.get("details", {})
        self.result.listing_complete = reconciled
        return self.result
