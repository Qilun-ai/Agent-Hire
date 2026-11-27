"""Native portal queries for the companies whose search endpoints we verified.

These fetchers ask the company's own search API with the parameters recorded in
``companies/<slug>/filters.json``. They return the portal's advertised count plus
the first ``pages`` of ids, which is enough to validate our local filtering
(counts and id overlap) without re-scraping every job body.

Used by ``scripts/filter_validation.py`` and ``scripts/filtered_run.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class NativeResult:
    company: str
    url: str
    count: int | None
    ids: list[str] = field(default_factory=list)
    pages: int = 0
    note: str = ""
    error: str | None = None


async def fetch_amazon(keyword: str, location: str | None = None,
                       pages: int = 3, client: httpx.AsyncClient | None = None) -> NativeResult:
    params = {"base_query": keyword, "result_limit": 100, "offset": 0, "sort": "recent"}
    if location:
        params["country"] = location  # ISO-3 for this portal
    own = client is None
    client = client or httpx.AsyncClient(timeout=40, follow_redirects=True, headers=UA)
    result = NativeResult("amazon", "https://www.amazon.jobs/en/search.json", None)
    try:
        for page in range(pages):
            params["offset"] = page * 100
            response = await client.get("https://www.amazon.jobs/en/search.json", params=params)
            response.raise_for_status()
            data = response.json()
            result.count = int(data.get("hits") or 0)
            result.pages += 1
            for job in data.get("jobs") or []:
                job_id = job.get("id_icims") or job.get("id")
                if job_id:
                    result.ids.append(str(job_id))
            if len(result.ids) >= result.count:
                break
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            await client.aclose()
    return result


async def fetch_microsoft(keyword: str, location: str | None = None,
                          pages: int = 9, client: httpx.AsyncClient | None = None) -> NativeResult:
    params: dict[str, Any] = {"domain": "microsoft.com", "query": keyword, "start": 0}
    if location:
        params["location"] = location
    own = client is None
    client = client or httpx.AsyncClient(timeout=40, follow_redirects=True, headers=UA)
    result = NativeResult("microsoft", "https://apply.careers.microsoft.com/api/pcsx/search", None)
    try:
        for page in range(pages):
            params["start"] = page * 10
            response = await client.get("https://apply.careers.microsoft.com/api/pcsx/search",
                                        params=params)
            response.raise_for_status()
            data = response.json().get("data") or {}
            result.count = int(data.get("count") or 0)
            result.pages += 1
            batch = data.get("positions") or []
            result.ids.extend(str(row.get("id")) for row in batch if row.get("id"))
            if not batch or len(result.ids) >= result.count:
                break
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            await client.aclose()
    return result


async def fetch_meesho(keyword: str = "", location: str | None = None,
                       pages: int = 1, client: httpx.AsyncClient | None = None) -> NativeResult:
    """Lever board: only ``location`` is server-side; keyword is not."""
    params = {"mode": "json"}
    if location:
        params["location"] = location
    own = client is None
    client = client or httpx.AsyncClient(timeout=40, follow_redirects=True, headers=UA)
    result = NativeResult("meesho", "https://api.lever.co/v0/postings/meesho", None)
    try:
        response = await client.get("https://api.lever.co/v0/postings/meesho", params=params)
        response.raise_for_status()
        rows = response.json()
        result.count = len(rows)
        result.pages = 1
        result.ids = [str(row.get("id")) for row in rows if row.get("id")]
        result.note = "Lever applies location only; keyword is client-side on the portal."
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            await client.aclose()
    return result


async def fetch_cohesity(keyword: str, location: str | None = None,
                         pages: int = 2, client: httpx.AsyncClient | None = None) -> NativeResult:
    url = "https://cohesity.wd5.myworkdayjobs.com/wday/cxs/cohesity/Cohesity_Careers/jobs"
    own = client is None
    client = client or httpx.AsyncClient(timeout=40, follow_redirects=True, headers=UA)
    result = NativeResult("cohesity", url, None)
    try:
        for page in range(pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                    "searchText": keyword}
            response = await client.post(url, json=body)
            response.raise_for_status()
            data = response.json()
            # Workday sometimes returns total=0 on later pages; the first page owns it.
            total = int(data.get("total") or 0)
            if result.count in (None, 0):
                result.count = total
            result.pages += 1
            for row in data.get("jobPostings") or []:
                path = row.get("externalPath") or ""
                job_id = path.rstrip("/").split("_")[-1] if "_" in path else path
                job_id = re.sub(r"-\d+$", "", job_id)
                if job_id:
                    result.ids.append(job_id)
            if len(result.ids) >= result.count:
                break
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if own:
            await client.aclose()
    return result


async def fetch_google_count(keyword: str, location: str | None = None,
                             pages: int = 1, **_kwargs) -> NativeResult:
    """Google renders the result count client-side; read it from the live page."""
    from . import access

    url = ("https://www.google.com/about/careers/applications/jobs/results/"
           f"?q={httpx.QueryParams({'q': keyword})['q']}")
    if location:
        url += f"&location={httpx.QueryParams({'l': location})['l']}"
    result = NativeResult("google", url, None)
    page = await access.fetch_with_stealth(url, wait_ms=15000)
    if page is None or not page.ok:
        result.error = "stealth render failed"
        return result
    import re
    match = re.search(r"([\d,]+)\s+jobs matched", page.html, re.I)
    if not match:
        match = re.search(r"([\d,]+)\s+jobs matched", _visible(page.html), re.I)
    result.count = int(match.group(1).replace(",", "")) if match else None
    result.note = "count only; no id list captured"
    return result


def _visible(html: str) -> str:
    import re
    text = re.sub(r"<(script|style)[^>]*>.*?</\\1>", " ", html or "", flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", text)


NATIVE_FETCHERS = {
    "amazon": fetch_amazon,
    "microsoft": fetch_microsoft,
    "meesho": fetch_meesho,
    "cohesity": fetch_cohesity,
    "google": fetch_google_count,
}


def page_budget(count: int | None, page_size: int, cap: int) -> int:
    if not count:
        return 1
    return min(cap, max(1, math.ceil(count / page_size)))
