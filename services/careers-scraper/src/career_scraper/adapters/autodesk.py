"""Autodesk's official careers site: jobs.autodesk.com (Eightfold PCSX).

The reviewed journey is the company's own careers site, not the marketing page
``www.autodesk.com/careers`` (Akamai-blocked from this network, and only a
redirect shell). ``jobs.autodesk.com/careers`` loads an Eightfold PCSX client
whose own public requests are:

* ``GET /api/pcsx/search?domain=autodesk.com&query=&location=&start=<n>``
  -> ``data.count`` plus ten ``data.positions`` per page
* ``GET /api/pcsx/position_details?position_id=<id>&domain=autodesk.com&hl=en``
  -> the full ``data.jobDescription`` HTML and the public role metadata

No ATS slug is guessed, no parent-wide board is used, and no job is invented:
membership is the site's advertised ``count`` reconciled against the unique
``position_id`` set, and every description comes from the matching detail
response for that same id.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from urllib.parse import urljoin

from .base import Adapter


BASE = "https://jobs.autodesk.com"
SEARCH_URL = BASE + "/api/pcsx/search"
DETAIL_URL = BASE + "/api/pcsx/position_details"
DOMAIN = "autodesk.com"
MAX_PAGES = 1000


def posted_date(timestamp) -> str | None:
    """PCSX ``postedTs`` is epoch seconds; unknown stays ``None``."""
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


class Autodesk(Adapter):
    """Collect Autodesk's official Eightfold PCSX listing and exact details."""

    VERIFICATION_REVISION = "autodesk.eightfold-pcsx-search-detail.1"

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": self.company.careers_url,
            "listing_api": SEARCH_URL,
            "detail_api": DETAIL_URL,
            "membership_authority": (
                "jobs.autodesk.com is Autodesk's own careers site (reached from "
                "https://www.autodesk.com/careers); membership is its PCSX "
                "search count and the unique position_id set it returns."
            ),
            "ats_usage": (
                "Eightfold PCSX is the engine of the company's own careers site, "
                "not a guessed third-party board."
            ),
        })
        try:
            positions, pages = await self._listing()
        except Exception as exc:
            self.result.errors.append(
                f"autodesk_pcsx_search_failed: {type(exc).__name__}: {exc}"
            )
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.timings["listing_phase_seconds"] = round(
                time.perf_counter() - started, 6
            )
            return self.result

        self.result.pages = pages
        # Advertised count, not the number we managed to page through: a short
        # crawl must stay visibly incomplete.
        self.result.expected_total = self.result.metadata.get("advertised_count") or len(positions)
        jobs = [self._job(position) for position in positions]
        self.result.jobs = jobs
        await self.hydrate(jobs, self._detail)

        verified = sum(1 for job in jobs if job.description_verified)
        self.result.listing_complete = (
            len(jobs) == self.result.expected_total
            and len({job.raw.get("id") for job in jobs}) == len(jobs)
            and verified == len(jobs)
            and not any(e.startswith("autodesk_pcsx_membership_incomplete") for e in self.result.errors)
        )
        overlay = self.result.metadata.get("access_attempts") or {}
        self.result.metadata["descriptions_verified"] = verified
        self.result.metadata["detail_requests"] = len(jobs)
        self.result.metadata["access_attempts"] = overlay
        self.result.timings["listing_phase_seconds"] = round(
            time.perf_counter() - started, 6
        )
        return self.result

    async def _page(self, start: int):
        payload = await self.http.json(SEARCH_URL, params={
            "domain": DOMAIN, "query": "", "location": "", "start": start,
        })
        if not isinstance(payload, dict) or payload.get("status") != 200:
            raise ValueError(f"PCSX search returned {payload!r}")
        data = payload.get("data") or {}
        batch = data.get("positions")
        count = data.get("count")
        if not isinstance(batch, list) or not isinstance(count, int):
            raise ValueError("PCSX search payload is missing positions/count")
        return batch, count

    async def _listing(self):
        """Walk PCSX search pages and reconcile the advertised count.

        Live insertions can shift the offset window between requests, which
        repeats an id and can push one past the final offset. Page normally,
        then re-scan with overlapping windows (step 5, then 3) until the unique
        id set matches the advertised count. ``listing_complete`` is only true
        at an exact match; a short crawl keeps the advertised total visible.
        """
        positions: dict[str, dict] = {}
        expected = None
        pages = 0
        passes = 0
        for step in (10, 10, 5, 3):
            offset = 0
            new_in_pass = 0
            passes += 1
            while pages < MAX_PAGES:
                batch, count = await self._page(offset)
                pages += 1
                if expected is None:
                    expected = count
                elif count != expected:
                    raise ValueError(
                        f"PCSX count changed during pagination: {expected} -> {count}"
                    )
                if not batch:
                    break
                for position in batch:
                    position_id = str(position.get("id"))
                    if position_id not in positions:
                        positions[position_id] = position
                        new_in_pass += 1
                if len(positions) >= expected:
                    break
                offset += step
                if offset >= expected + step:
                    break
            if expected is not None and len(positions) >= expected:
                break
            if new_in_pass == 0 and step == 3:
                break
        if expected is None:
            raise ValueError("PCSX search never returned a count")
        self.result.metadata["advertised_count"] = expected
        self.result.metadata["listing_passes"] = passes
        if len(positions) < expected:
            self.result.errors.append(
                f"autodesk_pcsx_membership_incomplete: count={expected}, "
                f"unique={len(positions)} after {passes} passes"
            )
        ordered = sorted(positions.values(), key=lambda row: row.get("id") or 0, reverse=True)
        return ordered, pages

    def _job(self, position: dict):
        position_id = str(position.get("id"))
        public = position.get("positionUrl") or f"/careers/job/{position_id}"
        url = urljoin(BASE + "/", public.lstrip("/"))
        return self.job(
            dict(position),
            title=position.get("name") or "",
            url=url,
            job_id=str(position.get("displayJobId") or position_id),
            requisition_id=str(position.get("atsJobId") or position_id),
            locations=list(position.get("locations") or []),
            department=position.get("department"),
            workplace_type=position.get("workLocationOption"),
            date_posted=posted_date(position.get("postedTs")),
            date_posted_raw=position.get("postedTs"),
        )

    async def _detail(self, job):
        """Fill one job from the exact detail response for its own position id."""
        position_id = str(job.raw.get("id"))
        payload = await self.http.json(DETAIL_URL, params={
            "position_id": position_id, "domain": DOMAIN, "hl": "en",
        })
        if not isinstance(payload, dict) or payload.get("status") != 200:
            raise ValueError(f"PCSX detail returned {payload!r}")
        data = payload.get("data") or {}
        if str(data.get("id")) != position_id:
            raise ValueError(
                f"PCSX detail id {data.get('id')!r} does not match requested {position_id}"
            )
        description = data.get("jobDescription") or ""
        job.description_html = description
        job.description_method = "autodesk.eightfold.pcsx.position_details.jobDescription"
        job.description_source_url = (
            f"{DETAIL_URL}?position_id={position_id}&domain={DOMAIN}&hl=en"
        )
        job.apply_url = data.get("publicUrl") or job.url
        job.raw = {**job.raw, "detail": data}
        if data.get("locations"):
            job.locations = list(data["locations"])
        job.date_posted = posted_date(data.get("postedTs")) or job.date_posted
        job.date_posted_raw = data.get("postedTs") or job.date_posted_raw
        job.department = data.get("department") or job.department
        job.workplace_type = data.get("workLocationOption") or job.workplace_type
        return job
