"""Samsung Research (Samsung R&D) careers hub and the company-scoped listings it publishes.

``https://research.samsung.com/careers`` is the official Samsung Research careers
hub.  It does not host a job listing itself; it publishes "Our Locations and
Application Channels", one row per Samsung R&D institute.  Only the rows that
expose a company-scoped listing are collected here:

* SR America -> ``https://sra.samsung.com/careers/``, whose own careers app is
  backed by the two Samsung Research America Greenhouse boards
  (``samsungresearchamerica`` and ``samsungresearchamericainternship``).
* SR India-Bangalore -> the exact Samsung_Careers Workday search the hub
  publishes, scoped by the published ``hiringCompany`` + Bangalore location
  facets.
* SR United Kingdom -> the exact Samsung_Careers Workday search the hub
  publishes, scoped by its published Staines location facet (currently empty).
* SR Poland -> ``https://samsungrd.pl``, whose company offers list is currently
  empty.

The remaining hub rows are documented but not collected because they are not a
company-scoped listing: group-wide Samsung careers sites, third-party job
aggregators, e-mail-only channels, keyword searches, or a single job detail URL.
The SR Japan site is recorded as a failed source when it is unreachable.

No parent-wide Samsung Electronics board is enumerated: the Workday requests
always carry the exact facets the Samsung Research hub publishes.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://research.samsung.com/careers"
SRA_CAREERS = "https://sra.samsung.com/careers/"
SRA_PUBLISHED_ENTRY = "https://www.sra.samsung.com/open-positions/#/"
SRA_BOARDS = ("samsungresearchamerica", "samsungresearchamericainternship")
SRA_INTERN_BOARD = "samsungresearchamericainternship"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}"

WORKDAY_ORIGIN = "https://sec.wd3.myworkdayjobs.com"
WORKDAY_SITE = "Samsung_Careers"
WORKDAY_LISTING_API = WORKDAY_ORIGIN + "/wday/cxs/sec/Samsung_Careers/jobs"
WORKDAY_DETAIL_PREFIX = WORKDAY_ORIGIN + "/wday/cxs/sec/Samsung_Careers"

SRIB_PUBLISHED_LISTING = (
    "https://sec.wd3.myworkdayjobs.com/en-US/Samsung_Careers"
    "?locations=189767dd6c9201b802603c83a5292c78"
    "&Location_Country=c4f78be1a8f14da0ab49ce1162348a5e"
    "&hiringCompany=189767dd6c9201554f3e5312a7290a85"
)
SRIB_FACETS = {
    "locations": ["189767dd6c9201b802603c83a5292c78"],
    "Location_Country": ["c4f78be1a8f14da0ab49ce1162348a5e"],
    "hiringCompany": ["189767dd6c9201554f3e5312a7290a85"],
}
SRUK_PUBLISHED_LISTING = (
    "https://sec.wd3.myworkdayjobs.com/en-US/Samsung_Careers"
    "?locations=189767dd6c9201d01d574c89a529dc7f"
)
SRUK_FACETS = {"locations": ["189767dd6c9201d01d574c89a529dc7f"]}
SRPOLAND_HOME = "https://samsungrd.pl"
SRJAPAN_CAREER = "https://www.samsung-srj.co.jp/career/"

# The visible Global R&D Centers tab of the hub.  The Global AI Centers tab is
# currently replaced by "We are updating the page!" and its rows are HTML
# comments, so it is not part of the current journey.
EXPECTED_CHANNELS = (
    "SR Korea(HQ)", "SR America", "SR United Kingdom", "SR Poland", "SR Ukraine",
    "SR Israel", "SR Jordan", "SR India-Bangalore", "SR Bangladesh",
    "SR Philippines", "SR Indonesia", "SR China-Beijing", "SR China-Nanjing",
    "SR Japan",
)
PUBLISHED_CHANNEL_LINKS = {
    "SR America": SRA_PUBLISHED_ENTRY,
    "SR United Kingdom": SRUK_PUBLISHED_LISTING.replace("en-US/", ""),
    "SR Poland": SRPOLAND_HOME,
    "SR India-Bangalore": SRIB_PUBLISHED_LISTING,
    "SR Japan": SRJAPAN_CAREER,
}
CHANNEL_DISPOSITIONS = {
    "SR Korea(HQ)": ("parent_wide_excluded",
                     "samsungcareers.com is Samsung Electronics' group-wide recruitment site; the hub publishes no SR-scoped listing there."),
    "SR America": ("collected", "Samsung Research America owns this listing; the hub links it and its careers app requests its two Greenhouse boards."),
    "SR United Kingdom": ("collected_empty", "Exact hub-published Samsung_Careers search scoped to the Staines location facet; currently returns zero postings."),
    "SR Poland": ("collected_empty", "Official Samsung R&D Institute Poland site; its offers list is currently empty."),
    "SR Ukraine": ("parent_wide_excluded", "The hub links the unfiltered ko-KR Samsung_Careers board, which is Samsung Electronics-wide and not SR-scoped."),
    "SR Israel": ("parent_wide_excluded", "The hub links the unfiltered ko-KR Samsung_Careers board, which is Samsung Electronics-wide and not SR-scoped."),
    "SR Jordan": ("parent_wide_excluded", "The hub links the unfiltered ko-KR Samsung_Careers board, which is Samsung Electronics-wide and not SR-scoped."),
    "SR India-Bangalore": ("collected", "Exact hub-published Samsung_Careers search scoped by hiringCompany=SRI-B and its Bangalore location facet."),
    "SR Bangladesh": ("aggregator_excluded", "The published link is a single bdjobs.com job detail on a third-party job board, not a company listing."),
    "SR Philippines": ("search_excluded", "The published link is an unfiltered keyword search ('Science Hub Tower 2') on the parent Workday site, not a membership listing."),
    "SR Indonesia": ("detail_excluded", "The published link is one Workday job detail, not a listing; a single sample cannot define membership."),
    "SR China-Beijing": ("aggregator_excluded", "The published link is a liepin.com company page on a third-party job board."),
    "SR China-Nanjing": ("aggregator_excluded", "The published link is a zhipin.com company page on a third-party job board."),
    "SR Japan": ("blocked", "The official SR Japan careers site returns HTTP 502 (www) / 403 (apex); recorded as a failed source."),
}

PAY_RANGE = re.compile(
    r"Base Pay Range\s*\$([\d,]+)\s*(?:—|–|-|to)\s*\$([\d,]+)\s*([A-Z]{3})?",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_official_channels(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse the visible Global R&D Centers tab of Samsung Research's careers hub."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "research.samsung.com" or parsed.path.rstrip("/") != "/careers":
        raise ValueError("Samsung Research careers entry redirected away from its inspected official URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "CAREERS" not in title.upper() or "Samsung Research" not in title:
        raise ValueError("Samsung Research careers page identity changed")
    boxes = soup.select(".careers__Con1 .careers__listBox")
    if len(boxes) != len(EXPECTED_CHANNELS):
        raise ValueError(
            f"Samsung Research careers hub now exposes {len(boxes)} R&D center rows, expected {len(EXPECTED_CHANNELS)}"
        )
    channels = []
    for box in boxes:
        name_node = box.select_one(".careers__imgBox span")
        website = box.select_one(".careers__webSite a")
        if name_node is None or website is None:
            raise ValueError("Samsung Research careers hub channel row is missing its name or website")
        name = _clean(name_node.get_text(" ", strip=True))
        prefix = _clean(name_node.find_previous("div", class_="careers__label").get_text(" ", strip=True))
        emails = [value for value in (EMAIL.findall(box.get_text(" ", strip=True)))]
        location_node = box.select_one(".careers__maps a")
        channels.append({
            "name": name,
            "region": prefix,
            "website": website.get("href"),
            "emails": emails,
            "location": _clean(location_node.get_text(" ", strip=True)) if location_node else None,
        })
    names = tuple(channel["name"] for channel in channels)
    if names != EXPECTED_CHANNELS:
        raise ValueError(f"Samsung Research careers hub channel set/order changed: {names}")
    by_name = {channel["name"]: channel for channel in channels}
    for name, expected in PUBLISHED_CHANNEL_LINKS.items():
        actual = by_name[name]["website"]
        if name == "SR United Kingdom":
            if urlsplit(actual or "") != urlsplit(expected):
                raise ValueError(f"Samsung Research UK channel link changed: {actual}")
        elif (actual or "").rstrip("/") != expected.rstrip("/"):
            raise ValueError(f"Samsung Research {name} channel link changed: {actual}")
    visible_text = _clean(soup.get_text(" ", strip=True))
    return {
        "url": response_url,
        "title": title,
        "channels": channels,
        "channel_count": len(channels),
        "legacy_job_list_visible": "Open Position" in visible_text,
        "ai_centers_tab_hidden_message": "We are updating the page!" in visible_text,
    }


def classify_channels(channels: list[dict]) -> list[dict]:
    result = []
    for channel in channels:
        disposition, reason = CHANNEL_DISPOSITIONS[channel["name"]]
        result.append({**channel, "disposition": disposition, "reason": reason})
    return result


def parse_sra_offices(payload: dict, board: str) -> list[dict]:
    """The jobs embedded in the exact /offices request made by SRA's careers app."""
    if board not in SRA_BOARDS:
        raise ValueError(f"Unexpected Samsung Research America board: {board}")
    stubs, seen = [], set()
    for office in payload.get("offices", []):
        for department in office.get("departments", []):
            for job in department.get("jobs", []):
                identity = str(job.get("id") or "")
                if not identity.isdigit():
                    raise ValueError(f"Samsung Research America {board} office job has no numeric id")
                if identity in seen:
                    continue
                seen.add(identity)
                location = job.get("location")
                stubs.append({
                    "id": identity,
                    "title": _clean(job.get("title")),
                    "office": office.get("name"),
                    "department": department.get("name"),
                    "location": location.get("name") if isinstance(location, dict) else location,
                })
    return stubs


def parse_sra_board(payload: dict, board: str) -> list[dict]:
    """Validate one Samsung Research America Greenhouse board response."""
    if board not in SRA_BOARDS:
        raise ValueError(f"Unexpected Samsung Research America board: {board}")
    expected_company = ("Samsung Research America Internship" if board == SRA_INTERN_BOARD
                        else "Samsung Research America")
    rows = payload.get("jobs") or []
    total = (payload.get("meta") or {}).get("total")
    if total is not None and int(total) != len(rows):
        raise ValueError(f"Samsung Research America {board} board total {total} != {len(rows)} rows")
    ids = set()
    for row in rows:
        identity = str(row.get("id") or "")
        if not identity.isdigit() or identity in ids:
            raise ValueError(f"Samsung Research America {board} board id is missing or duplicated: {identity}")
        ids.add(identity)
        if row.get("company_name") != expected_company:
            raise ValueError(
                f"Samsung Research America {board} board changed company: {row.get('company_name')!r}"
            )
        if not (row.get("content") or "").strip():
            raise ValueError(f"Samsung Research America {board} job {identity} has no content")
        if not str(row.get("absolute_url") or "").startswith(
                f"https://job-boards.greenhouse.io/{board}/jobs/"):
            raise ValueError(f"Samsung Research America {board} job {identity} has an unexpected apply URL")
    return rows


def sra_detail_url(identity: str, board: str) -> str:
    job_type = "intern" if board == SRA_INTERN_BOARD else "fte"
    return f"{SRA_CAREERS}detail/?id={identity}&type={job_type}"


def extract_pay_range(html: str | None) -> dict | None:
    match = PAY_RANGE.search(text_html(html))
    if match is None:
        return None
    minimum, maximum, currency = match.groups()
    return {
        "min": int(minimum.replace(",", "")),
        "max": int(maximum.replace(",", "")),
        "currency": currency or "USD",
        "source_text": _clean(match.group(0)),
    }


def parse_sra_job(job: dict, board: str) -> dict:
    """Normalise one exact Greenhouse job record from the SRA boards."""
    identity = str(job.get("id"))
    metadata = {item.get("name"): item.get("value") for item in job.get("metadata") or []}
    departments = [item.get("name") for item in job.get("departments") or [] if item.get("name")]
    offices = [item.get("name") for item in job.get("offices") or [] if item.get("name")]
    location = (job.get("location") or {}).get("name") if isinstance(job.get("location"), dict) else None
    description = job.get("content") or ""
    return {
        "id": identity,
        "title": _clean(job.get("title")),
        "url": sra_detail_url(identity, board),
        "apply_url": job.get("absolute_url"),
        "requisition_id": str(job["requisition_id"]) if job.get("requisition_id") else None,
        "location": location,
        "offices": offices,
        "departments": departments,
        "department": departments[0] if departments else None,
        "employment_type": metadata.get("Employment Type"),
        "job_category": metadata.get("Job Category"),
        "skills": metadata.get("Main Skills/Competencies") or [],
        "remote_option": metadata.get("Remote Work Option"),
        "date_posted": job.get("first_published"),
        "date_updated": job.get("updated_at"),
        "valid_through": job.get("application_deadline"),
        "language": job.get("language"),
        "employer": job.get("company_name"),
        "salary": extract_pay_range(description),
        "description_html": description,
        "description_sha256": sha256(text_html(description).encode()).hexdigest() if description else None,
        "talent_community_intake": not bool(job.get("requisition_id")) and _clean(job.get("title")).lower().startswith("don't see"),
    }


def parse_workday_listing(payload: dict, facets: dict, published_url: str, offset: int = 0) -> dict:
    """Read one exact company-scoped Workday search response (never the parent board)."""
    if (facets, published_url) not in ((SRIB_FACETS, SRIB_PUBLISHED_LISTING),
                                      (SRUK_FACETS, SRUK_PUBLISHED_LISTING)):
        raise ValueError("Samsung R&D Workday request is not one of the exact hub-published searches")
    total = payload.get("total")
    rows = payload.get("jobPostings") or []
    if not isinstance(total, int):
        raise ValueError("Samsung R&D Workday listing has no integer total")
    if offset == 0 and total > len(rows) and len(rows) != 20:
        raise ValueError(f"Samsung R&D Workday listing total {total} does not reconcile with {len(rows)} rows")
    for row in rows:
        path = row.get("externalPath") or ""
        if not path.startswith("/job/") or urlsplit(path).netloc:
            raise ValueError(f"Samsung R&D Workday row has an unexpected externalPath: {path!r}")
        if not _clean(row.get("title")):
            raise ValueError(f"Samsung R&D Workday row has no title: {path}")
    return {
        "total": total,
        "rows": rows,
        "facets": facets,
        "published_url": published_url,
        "job_page_urls": [f"{WORKDAY_ORIGIN}/en-US/{WORKDAY_SITE}{row['externalPath']}" for row in rows],
    }


def parse_workday_job(payload: dict, path: str) -> dict:
    info = payload.get("jobPostingInfo") or {}
    expected_id = path.rstrip("/").rsplit("/", 1)[-1]
    if info.get("jobPostingId") != expected_id:
        raise ValueError(f"Samsung R&D Workday detail posting id mismatch for {path}")
    description = info.get("jobDescription") or ""
    if len(text_html(description)) < 100:
        raise ValueError(f"Samsung R&D Workday detail has a short/empty description: {path}")
    employer = (payload.get("hiringOrganization") or {}).get("name")
    return {
        "title": _clean(info.get("title")),
        "requisition_id": info.get("jobReqId"),
        "job_posting_id": info.get("jobPostingId"),
        "apply_url": info.get("externalUrl"),
        "location": info.get("location"),
        "additional_locations": info.get("additionalLocations") or [],
        "date_posted": info.get("startDate"),
        "date_posted_raw": info.get("postedOn"),
        "valid_through": info.get("endDate"),
        "valid_through_text": info.get("jobPostingEndDateAsText"),
        "time_left_to_apply": info.get("timeLeftToApply"),
        "employment_type": info.get("timeType"),
        "workplace_type": info.get("remoteType"),
        "country": (info.get("country") or {}).get("descriptor"),
        "employer": employer,
        "description_html": description,
        "description_sha256": sha256(text_html(description).encode()).hexdigest(),
        "salary": extract_pay_range(description),
    }


def parse_poland_offers(html: str) -> dict:
    """Read the server-rendered Samsung R&D Institute Poland offers list."""
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one(".job_offers_list")
    if container is None:
        raise ValueError("Samsung R&D Institute Poland page no longer has its offers list container")
    cards = container.select(".job_offers_item")
    if cards:
        # No current offers were rendered when the listing was inspected, so no
        # card shape has been observed.  Fail loudly instead of reporting zero.
        raise ValueError(f"Samsung R&D Institute Poland now renders {len(cards)} offer cards; listing shape not inspected")
    return {
        "container_present": True,
        "offers": [],
        "offers_count": 0,
        "page_title": _clean(soup.title.get_text(" ", strip=True)) if soup.title else "",
    }


class SamsungRD(Adapter):
    """Collect every company-scoped listing the Samsung Research hub publishes."""

    VERIFICATION_REVISION = "samsung-r-d.research-hub.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._sources: dict[str, dict] = {}
        self._failed_sources: list[dict] = []

    async def run(self):
        if self.company.slug != "samsung-r-d":
            raise ValueError("SamsungRD adapter requires the samsung-r-d company")
        if self.source.url != OFFICIAL_ENTRY:
            raise ValueError(f"Samsung Research careers entry must be {OFFICIAL_ENTRY}")

        official_started = time.perf_counter()
        hub = await self.http.get(OFFICIAL_ENTRY)
        parsed_hub = parse_official_channels(hub.text, str(hub.url))
        channels = classify_channels(parsed_hub["channels"])
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - official_started)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": OFFICIAL_ENTRY,
            "official_entry_final_url": str(hub.url),
            "official_entry_title": parsed_hub["title"],
            "official_entry_channel_count": parsed_hub["channel_count"],
            "channel_rows": channels,
            "legacy_job_list_visible": parsed_hub["legacy_job_list_visible"],
            "ai_centers_tab_hidden": parsed_hub["ai_centers_tab_hidden_message"],
            "listing_authority": (
                "research.samsung.com/careers publishes per-institute application channels; "
                "only rows exposing a company-scoped listing are collected"
            ),
        })

        collection_started = time.perf_counter()
        await self._collect_sra(channels)
        await self._collect_workday(
            "SRIB", channels, SRIB_FACETS, SRIB_PUBLISHED_LISTING, expected_minimum=0,
        )
        await self._collect_workday(
            "SRUK", channels, SRUK_FACETS, SRUK_PUBLISHED_LISTING, expected_minimum=0,
        )
        await self._collect_poland(channels)
        await self._check_japan(channels)
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - collection_started)

        self.result.metadata["sources"] = self._sources
        self.result.metadata["failed_sources"] = self._failed_sources

        # hydrate() records its own measured detail_phase_seconds.
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)

        self.result.expected_total = len(self.result.jobs)

        recheck_started = time.perf_counter()
        try:
            await self._final_recheck(channels)
        except Exception as exc:
            self.result.metadata["final_membership_recheck"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.result.errors.append(f"samsung_r_d_final_membership_recheck_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - recheck_started)
        # Collected sources reconcile exactly; a blocked hub-published channel
        # (SR Japan) leaves umbrella membership closed for this snapshot only.
        self.result.listing_complete = not self._failed_sources and "sample_limit" not in self.result.warnings
        self.result.metadata["expected_total_by_source"] = {
            name: source["jobs"] for name, source in self._sources.items()
        }
        return self.result

    # ------------------------------------------------------------------ sources

    async def _collect_sra(self, channels: list[dict]):
        channel = next(item for item in channels if item["name"] == "SR America")
        if channel["disposition"] != "collected":
            raise ValueError("Samsung Research America channel is no longer collected")
        started = time.perf_counter()
        source = {
            "kind": "greenhouse",
            "published_url": SRA_PUBLISHED_ENTRY,
            "resolved_url": SRA_CAREERS,
            "boards": {},
            "jobs": 0,
            "talent_community_intakes": [],
            "visible_membership": [],
        }
        for board in SRA_BOARDS:
            offices_url = GREENHOUSE_API.format(board=board) + "/offices"
            jobs_url = GREENHOUSE_API.format(board=board) + "/jobs?content=true"
            if not self.page_guard():
                raise ValueError("Samsung Research America page budget exhausted")
            offices = await self.http.json(offices_url)
            stubs = parse_sra_offices(offices, board)
            if not self.page_guard():
                raise ValueError("Samsung Research America page budget exhausted")
            board_payload = await self.http.json(jobs_url)
            rows = {str(row["id"]): row for row in parse_sra_board(board_payload, board)}
            visible_ids = [stub["id"] for stub in stubs]
            missing = [identity for identity in visible_ids if identity not in rows]
            if missing:
                raise ValueError(f"Samsung Research America {board} UI ids missing from board: {missing}")
            extras = sorted(set(rows) - set(visible_ids))
            source["boards"][board] = {
                "offices_url": offices_url,
                "jobs_url": jobs_url,
                "board_total": len(rows),
                "ui_visible_jobs": len(stubs),
                "board_only_ids": extras,
                "ui_visible_ids": visible_ids,
            }
            source["visible_membership"].extend(
                {"id": stub["id"], "title": stub["title"], "board": board} for stub in stubs
            )
            for stub in stubs:
                row = rows[stub["id"]]
                job = parse_sra_job(row, board)
                if job["id"] != stub["id"] or job["title"] != stub["title"]:
                    raise ValueError(
                        f"Samsung Research America {board} UI/board identity mismatch for {stub['id']}"
                    )
                self.add(self.job(
                    {
                        "board": board,
                        "ui_stub": stub,
                        "greenhouse_job": row,
                        "source_url": jobs_url,
                        "detail_api_url": f"{GREENHOUSE_API.format(board=board)}/jobs/{stub['id']}",
                    },
                    title=job["title"], job_id=job["id"], requisition_id=job["requisition_id"],
                    url=job["url"], apply_url=job["apply_url"], description=job["description_html"],
                    method="greenhouse.boards_api.jobs.content",
                    description_url=f"{GREENHOUSE_API.format(board=board)}/jobs/{stub['id']}",
                    locations=[job["location"]] if job["location"] else job["offices"],
                    department=job["departments"], employment_type=job["employment_type"],
                    workplace_type=job["remote_option"], date_posted=job["date_posted"],
                    date_posted_raw=job["date_posted"], date_updated=job["date_updated"],
                    valid_through=job["valid_through"], salary=job["salary"],
                    employer=job["employer"], language=job["language"],
                ))
            for identity in extras:
                job = parse_sra_job(rows[identity], board)
                source["talent_community_intakes"].append({
                    "id": identity, "title": job["title"], "board": board, "url": job["url"],
                    "date_posted": job["date_posted"], "requisition_id": job["requisition_id"],
                    "reason": "Published 'Join our talent network' intake, not one of the visible listing results.",
                    "description_sha256": job["description_sha256"],
                })
            if self.limit():
                break
        source["jobs"] = len(source["visible_membership"])
        source["collection_seconds"] = max(0.0, time.perf_counter() - started)
        self._sources["samsung_research_america"] = source

    async def _collect_workday(self, key: str, channels: list[dict], facets: dict,
                               published_url: str, *, expected_minimum: int):
        name = {"SRIB": "SR India-Bangalore", "SRUK": "SR United Kingdom"}[key]
        channel = next(item for item in channels if item["name"] == name)
        started = time.perf_counter()
        source = {
            "kind": "workday_scoped_search",
            "published_url": published_url,
            "facets": facets,
            "jobs": 0,
            "listed_ids": [],
            "pages": 0,
        }
        offset, seen = 0, set()
        while True:
            if not self.page_guard():
                raise ValueError(f"Samsung R&D {name} Workday page budget exhausted")
            payload = await self.http.json(
                WORKDAY_LISTING_API, method="POST",
                json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""},
            )
            listing = parse_workday_listing(payload, facets, published_url, offset)
            total = listing["total"] if offset == 0 else source["expected_total"]
            source["pages"] += 1
            if source["pages"] == 1:
                source["expected_total"] = total
                if total < expected_minimum:
                    raise ValueError(f"Samsung R&D {name} Workday listing dropped below {expected_minimum} postings")
            rows = payload.get("jobPostings") or []
            for row in rows:
                path = row["externalPath"]
                if path in seen:
                    continue
                seen.add(path)
                requisition = next(
                    (value for value in row.get("bulletFields") or []
                     if isinstance(value, str) and re.fullmatch(r"[A-Za-z]*\d[\w-]*", value)),
                    path.rsplit("_", 1)[-1],
                )
                self.add(self.job(
                    {"workday_listing": row, "published_search": published_url,
                     "applied_facets": facets, "detail_api_url": WORKDAY_DETAIL_PREFIX + path},
                    title=_clean(row.get("title")),
                    job_id=path,
                    requisition_id=requisition,
                    url=f"{WORKDAY_ORIGIN}/en-US/{WORKDAY_SITE}{path}",
                    locations=[row.get("locationsText")] if row.get("locationsText") else [],
                    date_posted_raw=row.get("postedOn"),
                    workplace_type=row.get("remoteType"),
                ))
                source["listed_ids"].append({"path": path, "requisition": requisition,
                                             "title": _clean(row.get("title"))})
                if self.limit():
                    break
            if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
                break
            if offset + len(rows) >= total or not rows:
                break
            offset += len(rows)
        source["jobs"] = len(source["listed_ids"])
        source["collection_seconds"] = max(0.0, time.perf_counter() - started)
        if channel["disposition"] == "collected_empty" and source["jobs"]:
            channel["disposition"] = "collected"
        self._sources[key.lower()] = source

    async def _collect_poland(self, channels: list[dict]):
        channel = next(item for item in channels if item["name"] == "SR Poland")
        if channel["disposition"] != "collected_empty":
            raise ValueError("Samsung Research Poland channel is no longer an inspected empty listing")
        started = time.perf_counter()
        response = await self.http.get(
            SRPOLAND_HOME,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        parsed = parse_poland_offers(response.text)
        self._sources["samsung_research_poland"] = {
            "kind": "official_institute_site",
            "published_url": SRPOLAND_HOME,
            "final_url": str(response.url),
            "http_status": response.status_code,
            "container_present": parsed["container_present"],
            "offers": parsed["offers"],
            "jobs": 0,
            "collection_seconds": max(0.0, time.perf_counter() - started),
        }

    async def _check_japan(self, channels: list[dict]):
        channel = next(item for item in channels if item["name"] == "SR Japan")
        started = time.perf_counter()
        record = {"kind": "official_institute_site", "published_url": SRJAPAN_CAREER,
                  "jobs": 0, "collection_seconds": 0.0}
        try:
            response = await self.http.get(SRJAPAN_CAREER)
        except Exception as exc:  # FetchError carries the exact status/URL
            record.update({
                "status": "failed_source",
                "error": f"{type(exc).__name__}: {exc}",
                "reason": channel["reason"],
            })
            self._failed_sources.append({
                "url": SRJAPAN_CAREER,
                "reason": f"HTTP error while fetching the official SR Japan careers channel: {type(exc).__name__}: {exc}",
            })
        else:
            record.update({
                "status": "unparsed_source",
                "http_status": response.status_code,
                "final_url": str(response.url),
                "note": "SR Japan channel became reachable; its listing shape has not been inspected, so no zero-job substitute is claimed.",
            })
            self.result.errors.append("samsung_r_d_japan_channel_now_reachable_but_unparsed")
            self._failed_sources.append({
                "url": SRJAPAN_CAREER,
                "reason": f"HTTP {response.status_code} but SR Japan listing shape was never inspected; no membership claim made.",
            })
        record["collection_seconds"] = max(0.0, time.perf_counter() - started)
        self._sources["samsung_research_japan"] = record
        if channel["disposition"] == "blocked" and record["status"] == "failed_source":
            channel["disposition"] = "blocked_recorded"

    # -------------------------------------------------------------- final check

    async def _final_recheck(self, channels: list[dict]):
        """Re-read every collected listing after hydration and compare exact memberships."""
        collected_sra = {
            str(raw["greenhouse_job"]["id"])
            for job in self.result.jobs
            for raw in [job.raw] if "greenhouse_job" in raw
        }
        collected_workday = {
            key: {item["path"] for item in source["listed_ids"]}
            for key, source in self._sources.items() if source["kind"] == "workday_scoped_search"
        }
        current_sra, current_workday = set(), {}
        for board in SRA_BOARDS:
            offices = await self.http.json(GREENHOUSE_API.format(board=board) + "/offices")
            current_sra.update(stub["id"] for stub in parse_sra_offices(offices, board))
        for key, facets, published in (
                ("srib", SRIB_FACETS, SRIB_PUBLISHED_LISTING),
                ("sruk", SRUK_FACETS, SRUK_PUBLISHED_LISTING)):
            offset, paths, first_total = 0, set(), None
            while True:
                payload = await self.http.json(
                    WORKDAY_LISTING_API, method="POST",
                    json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""},
                )
                listing = parse_workday_listing(payload, facets, published, offset)
                if offset == 0:
                    first_total = listing["total"]
                rows = payload.get("jobPostings") or []
                paths.update(row["externalPath"] for row in rows)
                if not rows or offset + len(rows) >= (first_total or 0):
                    break
                offset += len(rows)
            current_workday[key] = paths
        poland = await self.http.get(
            SRPOLAND_HOME,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        current_poland = parse_poland_offers(poland.text)["offers_count"]
        hub = await self.http.get(OFFICIAL_ENTRY)
        current_channels = parse_official_channels(hub.text, str(hub.url))["channels"]
        recheck = {
            "sra_initial_ids": sorted(collected_sra),
            "sra_final_ids": sorted(current_sra),
            "sra_membership_matches": collected_sra == current_sra,
            "workday_initial_ids": {key: sorted(value) for key, value in collected_workday.items()},
            "workday_final_ids": {key: sorted(value) for key, value in current_workday.items()},
            "workday_membership_matches": collected_workday == current_workday,
            "poland_initial_offers": 0,
            "poland_final_offers": current_poland,
            "poland_membership_matches": current_poland == 0,
            "hub_channels_match": [item["name"] for item in current_channels] == [item["name"] for item in channels],
        }
        self.result.metadata["final_membership_recheck"] = recheck
        if not (recheck["sra_membership_matches"] and recheck["workday_membership_matches"]
                and recheck["poland_membership_matches"] and recheck["hub_channels_match"]):
            self.result.errors.append("samsung_r_d_membership_changed_during_run")
            self.result.listing_complete = False

    # ------------------------------------------------------------------- detail

    async def _detail(self, job):
        raw = job.raw
        if "workday_listing" in raw:
            path = raw["workday_listing"]["externalPath"]
            api = WORKDAY_DETAIL_PREFIX + path
            started = time.perf_counter()
            payload = await self.http.json(api)
            parsed = parse_workday_job(payload, path)
            job.raw["detail"] = payload
            job.title = parsed["title"]
            job.description_html = parsed["description_html"]
            job.description_method = "workday.jobPostingInfo.jobDescription"
            job.description_source_url = api
            job.requisition_id = parsed["requisition_id"] or job.requisition_id
            job.job_id = parsed["requisition_id"] or parsed["job_posting_id"] or job.job_id
            job.apply_url = parsed["apply_url"] or job.apply_url
            job.locations = [parsed["location"]] + [
                item for item in parsed["additional_locations"] if item and item != parsed["location"]
            ]
            job.date_posted = parsed["date_posted"]
            job.date_posted_raw = parsed["date_posted_raw"] or job.date_posted_raw
            job.valid_through = parsed["valid_through"]
            job.employment_type = parsed["employment_type"]
            job.workplace_type = parsed["workplace_type"]
            job.employer = parsed["employer"]
            job.salary = parsed["salary"]
            job.raw["detail_parse_seconds"] = max(0.0, time.perf_counter() - started)
            return job
        if "greenhouse_job" in raw:
            board = raw["board"]
            identity = str(raw["greenhouse_job"]["id"])
            api = raw["detail_api_url"]
            payload = await self.http.json(api)
            if str(payload.get("id")) != identity:
                raise ValueError(f"Samsung Research America detail id changed for {identity}")
            listing = parse_sra_job(raw["greenhouse_job"], board)
            detail = parse_sra_job(payload, board)
            if detail["description_sha256"] != listing["description_sha256"]:
                self.result.warnings.append(f"sra_listing_vs_detail_content_changed:{identity}")
            job.raw["detail"] = payload
            job.title = detail["title"]
            job.description_html = detail["description_html"]
            job.description_method = "greenhouse.boards_api.job_detail.content"
            job.description_source_url = api
            job.requisition_id = detail["requisition_id"]
            job.apply_url = detail["apply_url"] or job.apply_url
            job.date_posted = detail["date_posted"]
            job.date_updated = detail["date_updated"]
            job.valid_through = detail["valid_through"]
            job.salary = detail["salary"]
            job.employer = detail["employer"]
            job.department = detail["departments"]
            job.employment_type = detail["employment_type"]
            return job
        raise ValueError(f"Samsung R&D job has no inspected detail route: {job.url}")
