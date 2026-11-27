from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from ..models import Company, Job, Result, Source


@dataclass
class Options:
    max_jobs: int | None = None
    max_pages: int = 10000
    detail_concurrency: int = 2
    browser: bool = False
    checkpoint: Callable | None = None
    cached_jobs: dict = field(default_factory=dict)
    reuse_unavailable: bool = True
    previous_report: dict = field(default_factory=dict)


class Adapter:
    def __init__(self, http, company: Company, source: Source, options: Options):
        self.http, self.company, self.source, self.options = http, company, source, options
        self.result = Result(company.name, source.url, source.adapter)
        self.seen: set[str] = set()

    def job(self, raw, *, title, url, job_id=None, description="", method=None,
            description_url=None, **kwargs) -> Job:
        return Job(
            company=self.company.name, source=self.source.adapter, title=title or "", url=url or "",
            job_id=str(job_id) if job_id is not None else None, raw=raw,
            description_html=description or "", description_method=method,
            description_source_url=description_url, **kwargs,
        )

    def add(self, job: Job):
        job.company_scope_verified = self.company.scope == "company"
        if job.key not in self.seen:
            self.seen.add(job.key)
            self.result.validate_job(job)
            self.result.jobs.append(job)
            self.result.dirty_jobs.append(job)
            if self.options.checkpoint and len(self.result.jobs) % 25 == 0:
                self.options.checkpoint(self.result)

    def limit(self) -> bool:
        if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
            self.result.warnings.append("sample_limit")
            self.result.listing_complete = False
            return True
        return False

    async def hydrate(self, jobs, detail):
        started = time.perf_counter()
        sem = asyncio.Semaphore(self.options.detail_concurrency)
        stats = {"total": len(jobs), "checked": 0, "fetched_jobs": 0,
                 "verified_cache_hits": 0, "unavailable_cache_hits": 0, "failed": 0}
        self.result.metadata["details"] = stats

        completed = 0

        async def one(index, job):
            nonlocal completed
            async with sem:
                try:
                    cached = self.options.cached_jobs.get(job.url)
                    same_posting = cached and (cached.record_identity is None or cached.key == job.key)
                    if same_posting and cached.description_verified:
                        result = cached
                        stats["verified_cache_hits"] += 1
                    elif same_posting and self.options.reuse_unavailable and self.confirmed_unavailable(cached):
                        result = cached
                        stats["unavailable_cache_hits"] += 1
                    else:
                        stats["fetched_jobs"] += 1
                        result = await detail(job)
                except Exception as exc:
                    job.warnings.append("detail_fetch_failed")
                    self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
                    result = job
                    stats["failed"] += 1
                jobs[index] = result
                result.record_identity = job.key
                result.company_scope_verified = self.company.scope == "company"
                self.result.validate_job(result)
                self.result.dirty_jobs.append(result)
                completed += 1
                stats["checked"] = completed
                if self.options.checkpoint and completed % 25 == 0:
                    self.options.checkpoint(self.result)
                return result

        try:
            return await asyncio.gather(*(one(i, j) for i, j in enumerate(jobs)))
        finally:
            seconds = max(0.0, time.perf_counter() - started)
            stats["duration_seconds"] = round(seconds, 6)
            self.result.timings["detail_phase_seconds"] = self.result.timings.get("detail_phase_seconds", 0.0) + seconds

    def confirmed_unavailable(self, job):
        return bool(job.description_unavailable and job.description_method and job.description_source_url
                    and "detail_fetch_failed" not in job.warnings)

    def page_guard(self):
        if self.result.pages >= self.options.max_pages:
            self.result.errors.append("page_budget_exhausted")
            return False
        self.result.pages += 1
        return True

    async def run(self) -> Result:
        raise NotImplementedError
