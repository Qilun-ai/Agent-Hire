"""Samsung Semiconductor India Research's own SSIR careers listing and details.

The journey starts at Samsung Semiconductor's official careers entry, follows
the published India (SSIR) page, then uses the exact India tab request shape of
Samsung's own ``insightfinder`` job search.  The response exposes 26 SSIR
Workday postings.  Each posting's full description is read from the company's
own ``usJobDetail`` proxy (the request made by its job-detail page); the direct
Workday CXS route is only a same-job fallback.  No Samsung-wide or guessed
Workday board is enumerated.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://semiconductor.samsung.com/careers/"
OFFICIAL_CAREERS = "https://semiconductor.samsung.com/about-us/careers/"
SSIR_CAREERS = "https://semiconductor.samsung.com/about-us/careers/ssir/"
JOBS_PAGE = "https://semiconductor.samsung.com/about-us/careers/jobs/"
LISTING_API = "https://search.semiconductor.samsung.com/semi/insightfinder"
DETAIL_PAGE = "https://semiconductor.samsung.com/about-us/careers/job-detail/"
DETAIL_API = "https://semiconductor.samsung.com/aemapi/semi/usJobDetail"
WORKDAY_ORIGIN = "https://sec.wd3.myworkdayjobs.com"
WORKDAY_TENANT = "sec"
WORKDAY_SITE = "Samsung_Careers"
WORKDAY_CXS = f"{WORKDAY_ORIGIN}/wday/cxs/{WORKDAY_TENANT}/{WORKDAY_SITE}"
SITE_CODE = "semissir"
CATEGORY = "workday"
STAGE = "live"
REGION = "india"
PAGE_SIZE = 10
WORKDAY_LISTING_PATH = re.compile(
    r"^/en-US/Samsung_Careers/job/(?P<location>[^/]+)/(?P<posting>[^/?#]+)$"
)
WORKDAY_EXTERNAL_PATH = re.compile(
    r"^/Samsung_Careers/job/(?P<location>[^/]+)/(?P<posting>[^/?#]+)$"
)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
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


def _date_or_none(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        raise ValueError(f"Samsung detail {field} is not an ISO date: {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Samsung detail {field} is not a real date: {value!r}") from exc
    return value


def _workday_listing_parts(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    match = WORKDAY_LISTING_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "sec.wd3.myworkdayjobs.com" or match is None:
        raise ValueError(f"Samsung row is not an exact SSIR Workday listing URL: {url}")
    return match.group("location"), match.group("posting")


def _workday_external_parts(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    match = WORKDAY_EXTERNAL_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "sec.wd3.myworkdayjobs.com" or match is None:
        raise ValueError(f"Samsung detail external URL is not an exact Samsung Workday posting: {url}")
    return match.group("location"), match.group("posting")


def derive_cxs_url(workday_url: str) -> str:
    """Reproduce the company job-detail page's own Workday CXS URL transform."""
    parsed = urlsplit(workday_url)
    if parsed.scheme != "https" or parsed.hostname != "sec.wd3.myworkdayjobs.com":
        raise ValueError(f"Samsung Workday job URL has an unexpected host: {workday_url}")
    parts = parsed.path.split("/")
    try:
        job_index = next(index for index, value in enumerate(parts) if value.lower() == "job")
    except StopIteration as exc:
        raise ValueError(f"Samsung Workday job URL has no job segment: {workday_url}") from exc
    if job_index + 2 >= len(parts):
        raise ValueError(f"Samsung Workday job URL has no posting slug: {workday_url}")
    posting = parts[job_index + 2].split("?")[0]
    if not posting:
        raise ValueError(f"Samsung Workday job URL has an empty posting slug: {workday_url}")
    return f"{WORKDAY_CXS}/job/{posting}"


def detail_api_url(cxs_url: str) -> str:
    return DETAIL_API + "?" + urlencode({"pageUrl": cxs_url})


def detail_page_url(workday_url: str) -> str:
    return DETAIL_PAGE + "?" + urlencode({"pageUrl": workday_url, "region": REGION})


def listing_params(page: int) -> dict:
    """The exact India tab request emitted by the company job-list bundle."""
    return {
        "onlyfilter": "N",
        "filter": "",
        "sort": "Newest",
        "stage": STAGE,
        "pagetype": "page",
        "site": SITE_CODE,
        "category": CATEGORY,
        "q": "",
        "startno": PAGE_SIZE * (page - 1),
        "pageno": page,
        "num": PAGE_SIZE,
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "semiconductor.samsung.com"
            or parsed.path.rstrip("/") != "/about-us/careers"):
        raise ValueError("Samsung official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    links = {normalized(a.get("href")) for a in soup.select("a[href]")}
    ssir_links = sorted(x for x in links if x and x.startswith("/about-us/careers/ssir"))
    jobs_links = sorted(x for x in links if x and x.startswith("/about-us/careers/jobs"))
    if title != "Careers | About Us | Samsung Semiconductor Global":
        raise ValueError("Samsung official careers entry title changed")
    if "/about-us/careers/ssir/" not in ssir_links:
        raise ValueError("Samsung official careers entry no longer publishes the India (SSIR) page")
    if "/about-us/careers/jobs/" not in jobs_links:
        raise ValueError("Samsung official careers entry no longer publishes the jobs page")
    meta = soup.select_one('meta[name="description"]')
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "description": normalized(meta.get("content") if meta else ""),
        "ssir_links": ssir_links,
        "jobs_links": jobs_links,
    }


def parse_ssir_page(html: str, response_url: str = SSIR_CAREERS) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "semiconductor.samsung.com"
            or parsed.path.rstrip("/") != "/about-us/careers/ssir"):
        raise ValueError("Samsung SSIR page redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = normalized(canonical.get("href")) if canonical else ""
    cta = [a for a in soup.select('a[href="/about-us/careers/jobs/?region=india"]')]
    if title != "SSIR Careers | About Us | Samsung Semiconductor Global":
        raise ValueError("Samsung SSIR careers page title changed")
    if canonical_url.rstrip("/") != SSIR_CAREERS.rstrip("/"):
        raise ValueError("Samsung SSIR careers page canonical URL changed")
    if not cta:
        raise ValueError("Samsung SSIR page no longer publishes the India search-jobs CTA")
    return {
        "url": SSIR_CAREERS,
        "final_url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "cta_url": "/about-us/careers/jobs/?region=india",
        "cta_count": len(cta),
        "meta_description": normalized(
            (soup.select_one('meta[name="description"]') or {}).get("content", "")
        ),
    }


def parse_jobs_page(html: str, response_url: str = JOBS_PAGE) -> dict:
    parsed = urlsplit(response_url)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != "semiconductor.samsung.com"
            or parsed.path.rstrip("/") != "/about-us/careers/jobs"
            or query.get("region") != [REGION]):
        raise ValueError("Samsung jobs page response is not the inspected India listing page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    search_domain = soup.select_one("#searchDomain")
    current_path = soup.select_one("#currentPath")
    india_tab = soup.select_one("#reionCode_india")
    if title != "Search Jobs | About Us | Samsung Semiconductor Global":
        raise ValueError("Samsung search-jobs page title changed")
    if (search_domain is None or normalized(search_domain.get("value"))
            != "https://search.semiconductor.samsung.com/semi/"):
        raise ValueError("Samsung search-jobs page no longer publishes the inspected search domain")
    if (current_path is None or normalized(current_path.get("value"))
            != "/content/semiconductor/global/about-us/careers/jobs"):
        raise ValueError("Samsung search-jobs page currentPath changed")
    if india_tab is None or normalized(india_tab.get_text(" ", strip=True)) != "India":
        raise ValueError("Samsung search-jobs page India tab is missing")
    return {
        "url": JOBS_PAGE,
        "final_url": response_url,
        "title": title,
        "search_domain": normalized(search_domain.get("value")),
        "current_path": normalized(current_path.get("value")),
        "india_tab": "India",
        "region": REGION,
        "request_shape": listing_params(1),
    }


def parse_listing_page(value: dict, requested_page: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Samsung insightfinder response is not an object")
    response = value.get("response")
    if not isinstance(response, dict):
        raise ValueError("Samsung insightfinder response has no response object")
    if response.get("statusCode") != 200:
        raise ValueError(f"Samsung insightfinder statusCode changed: {response.get('statusCode')}")
    result = response.get("resultData")
    client = result.get("clientBrowser") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or response.get("siteCode") != SITE_CODE
            or (isinstance(client, dict) and client.get("siteCode") not in (None, SITE_CODE))):
        raise ValueError("Samsung insightfinder response is not the SSIR site")
    result_list = result.get("resultList")
    if not isinstance(result_list, list) or not result_list:
        raise ValueError("Samsung insightfinder response has no resultList")
    page_value = result_list[0]
    if page_value is None:
        return {
            "page": requested_page,
            "count": None,
            "rows": [],
            "items": 0,
            "terminal": True,
            "issues": [],
        }
    if not isinstance(page_value, dict):
        raise ValueError("Samsung insightfinder resultList first value is not an object")
    try:
        count = int(page_value["resultCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Samsung insightfinder resultCount is missing or invalid") from exc
    if count < 0:
        raise ValueError("Samsung insightfinder resultCount is negative")
    content = page_value.get("insightLandingContentList") or []
    if not isinstance(content, list):
        raise ValueError("Samsung insightfinder insightLandingContentList is not a list")
    if len(content) > PAGE_SIZE:
        raise ValueError("Samsung insightfinder returned more rows than its page size")
    rows, issues, seen = [], [], set()
    for raw in content:
        if not isinstance(raw, dict):
            issues.append("samsung_listing_row_not_object")
            continue
        identity = normalized(str(raw.get("workdayId") or ""))
        title = normalized(raw.get("workdayTitle"))
        workday_url = normalized(raw.get("workdayUrl") or raw.get("pageUrl"))
        location = normalized(raw.get("workdayLocation"))
        try:
            route_location, posting_id = _workday_listing_parts(workday_url)
        except ValueError as exc:
            issues.append(f"samsung_listing_row_bad_workday_url:{identity or 'unknown'}:{exc}")
            continue
        if not identity or not re.fullmatch(r"[A-Za-z]*\d[\w-]*", identity):
            issues.append(f"samsung_listing_row_missing_workday_id:{identity or 'unknown'}")
            continue
        if not title:
            issues.append(f"samsung_listing_row_missing_title:{identity}")
            continue
        if not location:
            issues.append(f"samsung_listing_row_missing_location:{identity}")
            continue
        if identity in seen:
            issues.append(f"samsung_listing_duplicate_workday_id:{identity}")
            continue
        if not route_location.startswith("SSIR"):
            issues.append(f"samsung_listing_row_outside_ssir_location_marker:{identity}:{route_location}")
        seen.add(identity)
        rows.append({
            **raw,
            "workdayId": identity,
            "workdayTitle": title,
            "workdayUrl": workday_url,
            "workdayLocation": location,
            "posting_id": posting_id,
            "route_location": route_location,
        })
    expected_rows = min(PAGE_SIZE, max(0, count - PAGE_SIZE * (requested_page - 1)))
    if len(rows) != expected_rows:
        issues.append(
            f"samsung_listing_page_row_count_mismatch:{requested_page}:{len(rows)}/{expected_rows}"
        )
    return {
        "page": requested_page,
        "count": count,
        "rows": rows,
        "items": len(rows),
        "terminal": False,
        "issues": issues,
    }


def _is_heading(block: Tag) -> str | None:
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return normalized(block.get_text(" ", strip=True)) or None
    if block.name not in {"p", "div"}:
        return None
    text = normalized(block.get_text(" ", strip=True))
    if not text or len(text) > 160:
        return None
    bold = block.find_all(["b", "strong"])
    if not bold:
        return None
    bold_text = normalized(" ".join(node.get_text(" ", strip=True) for node in bold))
    return text if bold_text and bold_text == text else None


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-samsung-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-samsung-body]")
    sections, current = [], None
    for block in list(root.children) if root else []:
        if not isinstance(block, Tag):
            continue
        heading = _is_heading(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": html or "", "text": ""})
    for section in sections:
        section["text"] = normalized_body_text(section["html"])
    return sections


def _section_matches(sections: list[dict], pattern: str) -> list[dict]:
    regex = re.compile(pattern, re.I)
    return [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section["heading"] and regex.search(section["heading"]) and section["text"]
    ]


def parse_detail(value: dict, expected_row: dict, requested_url: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Samsung job detail response is not an object")
    info = value.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Samsung job detail response has no jobPostingInfo")
    title = normalized(info.get("title"))
    expected_title = normalized(expected_row.get("workdayTitle"))
    if not title or title != expected_title:
        raise ValueError(f"Samsung detail title mismatch: {title!r} != {expected_title!r}")
    requisition = normalized(str(info.get("jobReqId") or ""))
    if not requisition or requisition != normalized(str(expected_row.get("workdayId") or "")):
        raise ValueError("Samsung detail jobReqId does not match the listed Workday ID")
    posting_id = normalized(str(info.get("jobPostingId") or ""))
    expected_posting = normalized(str(expected_row.get("posting_id") or ""))
    if not posting_id or posting_id != expected_posting:
        raise ValueError("Samsung detail jobPostingId does not match the listed Workday posting")
    external_url = normalized(info.get("externalUrl"))
    external_location, external_posting = _workday_external_parts(external_url)
    if external_posting != posting_id:
        raise ValueError("Samsung detail externalUrl does not identify the same posting")
    if not external_location.startswith("SSIR"):
        raise ValueError("Samsung detail externalUrl is outside the SSIR location marker")
    location = normalized(info.get("location"))
    if not location:
        raise ValueError("Samsung detail has no location")
    description_html = info.get("jobDescription")
    if not isinstance(description_html, str):
        raise ValueError("Samsung detail has no jobDescription HTML")
    description_text = normalized_body_text(description_html)
    if len(description_text) < 100:
        raise ValueError("Samsung detail description is implausibly short")
    start_date = _date_or_none(info.get("startDate"), "startDate")
    end_date = _date_or_none(info.get("endDate"), "endDate")
    additional = []
    for item in info.get("additionalLocations") or []:
        if isinstance(item, dict):
            item = item.get("descriptor") or item.get("name")
        item = normalized(item)
        if item and item not in additional:
            additional.append(item)
    locations = list(dict.fromkeys([location] + additional))
    organization = value.get("hiringOrganization") if isinstance(value.get("hiringOrganization"), dict) else {}
    employer = normalized(organization.get("name")) or None
    sections = description_sections(description_html)
    links = [
        {"text": normalized(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
        for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    apply_url = external_url.rstrip("/") + "/apply"
    return {
        "requested_url": requested_url,
        "title": title,
        "job_posting_id": posting_id,
        "requisition_id": requisition,
        "workday_id": normalized(expected_row.get("workdayId")),
        "external_url": external_url,
        "apply_url": apply_url,
        "location": location,
        "locations": locations,
        "employer": employer,
        "time_type": normalized(info.get("timeType")) or None,
        "remote_type": normalized(info.get("remoteType")) or None,
        "date_posted": start_date,
        "date_posted_raw": start_date,
        "valid_through": end_date,
        "dates": {
            "startDate": info.get("startDate"),
            "endDate": info.get("endDate"),
            "postedOn": info.get("postedOn"),
            "timeLeftToApply": info.get("timeLeftToApply"),
            "jobPostingEndDateAsText": info.get("jobPostingEndDateAsText"),
        },
        "posted": info.get("posted"),
        "can_apply": info.get("canApply"),
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_sections": sections,
        "responsibilities": _section_matches(
            sections, r"responsibilit|what you will do|what you'll do"
        ) or None,
        "qualifications": _section_matches(
            sections, r"qualif|skills|requirements|required skills|experience|education"
        ) or None,
        "experience": _section_matches(sections, r"experience") or None,
        "education": _section_matches(sections, r"education") or None,
        "description_links": links,
        "role_attachments": attachments,
    }


def _response_metadata(response, seconds: float) -> dict:
    return {
        "request_url": str(response.request.url),
        "final_url": str(response.url),
        "status": response.status_code,
        "seconds": max(0.0, seconds),
        "response_bytes": len(response.content),
        "response_sha256": sha256(response.content).hexdigest(),
    }


class SamsungSemiconductorIndiaResearch(Adapter):
    """Read only the SSIR postings exposed by Samsung Semiconductor's India tab."""

    VERIFICATION_REVISION = "samsung-semiconductor-india-research.ssir-insightfinder-workday.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(_response_metadata(response, seconds))
        return entry

    async def _ssir_page(self):
        started = time.perf_counter()
        response = await self.http.get(SSIR_CAREERS)
        seconds = max(0.0, time.perf_counter() - started)
        page = parse_ssir_page(response.text, str(response.url))
        page.update(_response_metadata(response, seconds))
        return page

    async def _jobs_page(self):
        started = time.perf_counter()
        response = await self.http.get(JOBS_PAGE + "?region=" + REGION)
        seconds = max(0.0, time.perf_counter() - started)
        page = parse_jobs_page(response.text, str(response.url))
        page.update(_response_metadata(response, seconds))
        return page

    async def _listing(self):
        started = time.perf_counter()
        pages, rows, seen = [], [], set()
        expected_count = total_pages = None
        issues = []
        page = 1
        terminal = None
        while True:
            if not self.page_guard():
                issues.append("samsung_listing_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.request(
                "GET", LISTING_API,
                params=listing_params(page),
                headers={
                    "Referer": JOBS_PAGE + "?region=" + REGION,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed = parse_listing_page(response.json(), page)
            issues.extend(parsed["issues"])
            if parsed["terminal"]:
                terminal = {
                    "page": page,
                    "count": None,
                    "rows": [],
                    "request_url": str(response.request.url),
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "request_seconds": request_seconds,
                    "response_bytes": len(response.content),
                    "response_sha256": sha256(response.content).hexdigest(),
                }
                break
            if expected_count is None:
                expected_count = parsed["count"]
                total_pages = max(0, (expected_count + PAGE_SIZE - 1) // PAGE_SIZE)
            elif parsed["count"] != expected_count:
                issues.append(f"samsung_listing_count_changed:{expected_count}->{parsed['count']}")
            pages.append({
                "page": page,
                "count": parsed["count"],
                "items": parsed["items"],
                "job_ids": [row["workdayId"] for row in parsed["rows"]],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            for row in parsed["rows"]:
                if row["workdayId"] in seen:
                    issues.append(f"samsung_listing_duplicate_id_across_pages:{row['workdayId']}")
                    continue
                seen.add(row["workdayId"])
                rows.append(row)
            if total_pages is None or total_pages < 1:
                issues.append("samsung_listing_page_count_unknown")
                break
            if page >= total_pages:
                break
            page += 1
        if terminal is None and expected_count is not None and not issues:
            # The page's next/View-more control disappears after the advertised
            # count.  Probe the next page to prove the request has no extra rows.
            if not self.page_guard():
                issues.append("samsung_terminal_probe_page_budget_exhausted")
            else:
                probe_page = (total_pages or 0) + 1
                probe_started = time.perf_counter()
                response = await self.http.request(
                    "GET", LISTING_API,
                    params=listing_params(probe_page),
                    headers={
                        "Referer": JOBS_PAGE + "?region=" + REGION,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                probe_seconds = max(0.0, time.perf_counter() - probe_started)
                parsed = parse_listing_page(response.json(), probe_page)
                terminal = {
                    "page": probe_page,
                    "count": parsed["count"],
                    "rows": parsed["rows"],
                    "request_url": str(response.request.url),
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "request_seconds": probe_seconds,
                    "response_bytes": len(response.content),
                    "response_sha256": sha256(response.content).hexdigest(),
                }
                if parsed["rows"]:
                    issues.append("samsung_terminal_probe_returned_unexpected_rows")
                issues.extend(parsed["issues"])
        if expected_count is not None and len(rows) != expected_count:
            issues.append(f"samsung_listing_membership_count:{len(rows)}/{expected_count}")
        if expected_count is not None and total_pages != len(pages):
            issues.append(f"samsung_listing_page_count:{len(pages)}/{total_pages}")
        return {
            "count": expected_count,
            "total_pages": total_pages,
            "rows": rows,
            "job_ids": [row["workdayId"] for row in rows],
            "pages": pages,
            "terminal": terminal,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _make_job(self, row: dict):
        posting_id = row["posting_id"]
        return self.job(
            {
                "listing": dict(row),
                "detail_page_url": detail_page_url(row["workdayUrl"]),
                "workday_cxs_url": derive_cxs_url(row["workdayUrl"]),
            },
            title=row["workdayTitle"],
            url=row["workdayUrl"],
            job_id=posting_id,
            requisition_id=row["workdayId"],
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=[row["workdayLocation"]],
            department=None,
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=row.get("workdayPostedOn"),
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=None,
            language="en",
        )

    async def _detail(self, job):
        row = job.raw["listing"]
        cxs_url = job.raw["workday_cxs_url"]
        api_url = detail_api_url(cxs_url)
        errors = []
        for source_kind, url in (("company_usJobDetail", api_url), ("workday_cxs_fallback", cxs_url)):
            try:
                started = time.perf_counter()
                response = await self.http.request(
                    "GET", url,
                    headers={
                        "Referer": job.raw["detail_page_url"],
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                seconds = max(0.0, time.perf_counter() - started)
                payload = response.json()
                parsed = parse_detail(payload, row, str(response.url))
                if source_kind == "workday_cxs_fallback":
                    job.warnings.append("samsung_company_detail_api_fallback")
                job.title = parsed["title"]
                job.job_id = parsed["job_posting_id"]
                job.requisition_id = parsed["requisition_id"]
                job.apply_url = parsed["apply_url"]
                job.description_html = parsed["description_html"]
                job.description_text = parsed["description_text"]
                job.description_method = (
                    "samsung.usJobDetail.jobPostingInfo.jobDescription"
                    if source_kind == "company_usJobDetail"
                    else "workday.cxs.jobPostingInfo.jobDescription"
                )
                job.description_source_url = str(response.url)
                job.locations = parsed["locations"]
                job.employer = parsed["employer"]
                job.employment_type = parsed["time_type"]
                job.workplace_type = parsed["remote_type"]
                job.date_posted = parsed["date_posted"]
                job.date_posted_raw = parsed["date_posted_raw"]
                job.date_updated = None
                job.valid_through = parsed["valid_through"]
                job.salary = None
                job.responsibilities = parsed["responsibilities"]
                job.qualifications = parsed["qualifications"]
                job.experience = parsed["experience"]
                job.education = parsed["education"]
                job.language = "en"
                job.raw["detail"] = {
                    "payload": payload,
                    "source_kind": source_kind,
                    "request_url": str(response.request.url),
                    "final_url": str(response.url),
                    "status": response.status_code,
                    "seconds": seconds,
                    "response_bytes": len(response.content),
                    "response_sha256": sha256(response.content).hexdigest(),
                    "description_sha256": parsed["description_sha256"],
                    "description_normalized_chars": parsed["description_normalized_chars"],
                    "description_fnv1a64": parsed["description_fnv1a64"],
                    "description_sections": parsed["description_sections"],
                    "description_links": parsed["description_links"],
                    "role_attachments": parsed["role_attachments"],
                    "dates": parsed["dates"],
                    "posted": parsed["posted"],
                    "can_apply": parsed["can_apply"],
                }
                if parsed["posted"] is False:
                    job.warnings.append("samsung_detail_not_posted")
                if parsed["can_apply"] is False:
                    job.warnings.append("samsung_detail_cannot_apply")
                return job
            except Exception as exc:
                errors.append(f"{source_kind}:{type(exc).__name__}:{exc}")
        raise ValueError("Samsung exact detail unavailable: " + " | ".join(errors))

    def _reconcile(self, listing: dict) -> dict:
        listing_ids = [row["posting_id"] for row in listing["rows"]]
        collected_ids = [job.job_id for job in self.result.jobs]
        missing = sorted(set(listing_ids) - set(collected_ids))
        extra = sorted(set(collected_ids) - set(listing_ids))
        duplicates = sorted({x for x in collected_ids if collected_ids.count(x) > 1})
        detail_mismatches = []
        for job in self.result.jobs:
            info = (job.raw.get("detail") or {}).get("payload", {}).get("jobPostingInfo")
            if not isinstance(info, dict):
                detail_mismatches.append({"job_id": job.job_id, "reason": "missing_jobPostingInfo"})
                continue
            if info.get("jobPostingId") != job.job_id or info.get("jobReqId") != job.requisition_id:
                detail_mismatches.append({"job_id": job.job_id, "reason": "detail_identity_changed"})
        complete = (
            listing.get("count") is not None
            and len(listing_ids) == listing["count"]
            and len(collected_ids) == listing["count"]
            and not missing
            and not extra
            and not duplicates
            and not listing.get("issues")
            and listing.get("terminal") is not None
            and not listing["terminal"]["rows"]
        )
        return {
            "advertised_count": listing.get("count"),
            "listing_pages": listing.get("total_pages"),
            "listing_page_rows": [len(page["job_ids"]) for page in listing.get("pages", [])],
            "listing_ids": listing_ids,
            "collected_ids": collected_ids,
            "missing_ids": missing,
            "extra_ids": extra,
            "duplicate_ids": duplicates,
            "detail_identity_mismatches": detail_mismatches,
            "terminal_probe_page": listing.get("terminal", {}).get("page") if listing.get("terminal") else None,
            "terminal_probe_rows": len(listing.get("terminal", {}).get("rows", [])) if listing.get("terminal") else None,
            "complete": complete,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_careers_url": OFFICIAL_CAREERS,
            "ssir_page_url": SSIR_CAREERS,
            "listing_page_url": JOBS_PAGE + "?region=" + REGION,
            "listing_url": LISTING_API,
            "listing_authority": (
                "Samsung Semiconductor's own India tab on its Search Jobs page; the site "
                "code semissir scopes the same public request the page bundle emits."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": "/semi/insightfinder",
                "query_shape": listing_params(1),
                "filters_applied": False,
                "scope": "site=semissir, category=workday, region=india only; no Samsung-wide or guessed Workday board.",
                "pagination": "10 rows per page; the India tab's View more repeats the same request with pageno/startno.",
            },
            "detail_method": (
                "The company's job-detail page calls /aemapi/semi/usJobDetail with the "
                "Workday CXS URL derived from the exact listed job URL; direct CXS is a "
                "same-job fallback only."
            ),
        })
        entry = await self._official_entry()
        ssir = await self._ssir_page()
        jobs_page = await self._jobs_page()
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.timings["ssir_page_seconds"] = ssir["seconds"]
        self.result.timings["jobs_page_seconds"] = jobs_page["seconds"]
        self.result.metadata.update({
            "official_entry": entry,
            "ssir_page": ssir,
            "jobs_page": jobs_page,
        })
        listing = await self._listing()
        self.result.expected_total = listing["count"]
        self.result.timings["listing_seconds"] = listing["request_seconds"]
        self.result.timings["source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.metadata["listing"] = listing
        for row in listing["rows"]:
            self.add(self._make_job(row))
        self.result.listing_complete = self._reconcile(listing)["complete"]
        if self.result.listing_complete is False:
            if listing["issues"]:
                self.result.errors.extend(listing["issues"])
            self.result.errors.append("samsung_listing_membership_not_reconciled")
        jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.jobs = jobs
        if any((job.raw.get("detail") or {}).get("seconds") for job in jobs):
            self.result.timings["detail_request_seconds_sum"] = sum(
                (job.raw.get("detail") or {}).get("seconds", 0.0) for job in jobs
            )
        reconciliation = self._reconcile(listing)
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["detail_failures"] = [
            {"job_id": job.job_id, "url": job.url, "warnings": job.warnings}
            for job in self.result.jobs
            if not job.description_verified
        ]
        if not reconciliation["complete"] and "samsung_listing_membership_not_reconciled" not in self.result.errors:
            self.result.errors.append("samsung_listing_membership_not_reconciled")
        return self.result
