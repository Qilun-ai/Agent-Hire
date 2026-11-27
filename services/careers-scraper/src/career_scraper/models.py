from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import re
import time
from typing import Any

from bs4 import BeautifulSoup
import httpx


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_html(value: str | None) -> str:
    value = value or ""
    # Decode an escaped *whole HTML wrapper* only. Unescaping normal HTML
    # before parsing turns literal code such as &lt;T&gt; into a lost HTML tag.
    for _ in range(2):
        start = r'^\s*&(?:amp;)*lt;(?:p|div|section|article|ul|ol|h[1-6])(?:\s|&(?:amp;)*gt;)'
        end = r'&(?:amp;)*lt;/(?:p|div|section|article|ul|ol|h[1-6])&(?:amp;)*gt;\s*$'
        if not (re.search(start, value, re.I) and re.search(end, value, re.I)):
            break
        value = unescape(value)
    soup = BeautifulSoup(value, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class Source:
    adapter: str
    url: str
    options: dict[str, Any] = field(default_factory=dict)
    evidence_url: str | None = None

    def __post_init__(self):
        if not isinstance(self.url, str) or not self.url.startswith(("http://", "https://")):
            return
        try:
            parsed = httpx.URL(self.url)
            # These public ATS endpoints use TLS; old embedded HTTP aliases otherwise duplicate the same board.
            known_tls = (parsed.host.endswith(".myworkdayjobs.com")
                         or bool(re.search(r"\.oraclecloud\d*\.com$", parsed.host)))
            if parsed.scheme == "http" and known_tls and parsed.port in (None, 80, 443):
                parsed = parsed.copy_with(scheme="https", port=None)
            # httpx also removes default :443/:80 and normalizes DNS case without changing path case.
            self.url = str(parsed)
        except httpx.InvalidURL:
            pass

    @property
    def key(self) -> str:
        return sha256(f"{self.adapter}:{self.url}".encode()).hexdigest()[:16]


@dataclass
class Company:
    name: str
    slug: str
    careers_url: str
    sources: list[Source] = field(default_factory=list)
    notes: str = ""
    scope: str = "company"
    enabled: bool = True
    source_aliases: dict[str, str] = field(default_factory=dict)

    def resolve_source(self, source: Source) -> Source:
        target = self.source_aliases.get(source.url)
        if not target:
            return source
        return Source(source.adapter, target, {**source.options, "observed_alias": source.url}, source.evidence_url)


@dataclass
class Job:
    company: str
    source: str
    title: str
    url: str
    job_id: str | None = None
    requisition_id: str | None = None
    apply_url: str | None = None
    description_html: str = ""
    description_text: str = ""
    locations: list[Any] = field(default_factory=list)
    department: Any = None
    team: Any = None
    employment_type: Any = None
    workplace_type: Any = None
    date_posted: str | None = None
    date_posted_raw: Any = None
    date_updated: str | None = None
    valid_through: str | None = None
    salary: Any = None
    qualifications: Any = None
    responsibilities: Any = None
    experience: Any = None
    education: Any = None
    employer: Any = None
    language: str | None = None
    description_source_url: str | None = None
    description_method: str | None = None
    description_verified: bool = False
    description_sha256: str | None = None
    fetched_at: str = field(default_factory=now)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    company_scope_verified: bool = True
    record_identity: str | None = None
    description_unavailable: bool = False

    @property
    def key(self) -> str:
        # Keep postings separate even if they share a requisition or title.
        if self.record_identity is None:
            self.record_identity = sha256(f"{self.company}|{self.source}|{self.job_id or ''}|{self.url}".encode()).hexdigest()
        return self.record_identity

    def validate(self) -> Job:
        if not self.description_text:
            self.description_text = text_html(self.description_html)
        issues = []
        if not self.title.strip():
            issues.append("missing_title")
        if not self.url.startswith(("http://", "https://")):
            issues.append("missing_job_url")
        body = self.description_text.strip()
        if len(body) < 100:
            issues.append("short_or_missing_description")
        if re.search(r"(?:\.\.\.|…)\s*(?:read more|show more|learn more)$", body, re.I):
            issues.append("possibly_truncated_description")
        if not self.description_method or not self.description_source_url:
            issues.append("no_full_description_provenance")
        generated = {"missing_title", "missing_job_url", "short_or_missing_description",
                     "possibly_truncated_description", "no_full_description_provenance"}
        self.warnings = list(dict.fromkeys([w for w in self.warnings if w not in generated] + issues))
        # Verified = full documented source field captured and sanity checks passed.
        # This is not a semantic/manual certification of everything the employer wrote.
        self.description_verified = not self.description_unavailable and not issues and not any(
            w in self.warnings for w in ("detail_fetch_failed", "description_not_full_field")
        )
        self.description_sha256 = sha256(body.encode()).hexdigest() if body else None
        return self

    def to_dict(self) -> dict:
        return {"record_id": self.key, **asdict(self)}


@dataclass
class Result:
    company: str
    source_url: str
    adapter: str
    jobs: list[Job] = field(default_factory=list)
    expected_total: int | None = None
    listing_complete: bool = False
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=now)
    finished_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    dirty_jobs: list[Job] = field(default_factory=list, repr=False)
    duration_seconds: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    _started_clock: float = field(default_factory=time.perf_counter, repr=False)

    def validate_job(self, job):
        started = time.perf_counter()
        try:
            return job.validate()
        finally:
            if self.duration_seconds is None:
                self.timings["local_validation_seconds"] = self.timings.get("local_validation_seconds", 0.0) + max(0.0, time.perf_counter() - started)

    def finish(self):
        if self.duration_seconds is None:
            self.duration_seconds = max(0.0, time.perf_counter() - self._started_clock)
            self.finished_at = now()

    def report(self, *, validate_jobs: bool = True) -> dict:
        jobs = self.jobs
        if validate_jobs:
            jobs = list({j.key: self.validate_job(j) for j in self.jobs}.values())
            self.jobs[:] = jobs
        if self.listing_complete and self.expected_total is not None and len(jobs) != self.expected_total:
            if "expected_total_is_capped_unfiltered_count" not in self.warnings:
                self.listing_complete = False
                if "listing_total_mismatch" not in self.errors:
                    self.errors.append("listing_total_mismatch")
        verified = sum(j.description_verified for j in jobs)
        complete = self.listing_complete and not self.errors and verified == len(jobs)
        status = "complete" if complete else "partial" if jobs else "failed"
        if "sample_limit" in self.warnings:
            status = "sampled"
        return {
            "company": self.company, "source_url": self.source_url, "adapter": self.adapter,
            "status": status, "listing_complete": self.listing_complete,
            "expected_total": self.expected_total, "jobs_collected": len(jobs),
            "descriptions_verified": verified, "pages": self.pages,
            "descriptions_unavailable": sum(j.description_unavailable for j in jobs),
            "errors": self.errors, "warnings": self.warnings,
            "metadata": self.metadata,
            "duration_seconds": round(self.duration_seconds if self.duration_seconds is not None else max(0.0, time.perf_counter() - self._started_clock), 6),
            "timings": {key: round(value, 6) for key, value in self.timings.items()},
            "started_at": self.started_at, "finished_at": self.finished_at or now(),
        }
