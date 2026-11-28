from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from . import access


class FetchError(RuntimeError):
    pass


class HTTP:
    """Rate-limited HTTP with bounded retries and content-addressed response evidence."""

    def __init__(self, output: Path | None = None, *, delay: float = .3,
                 concurrency: int = 10, timeout: float = 35, retries: int = 3,
                 transport=None, max_response_bytes: int = 64 * 1024 * 1024):
        self.output = output
        self.delay = delay
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self.sem = asyncio.Semaphore(concurrency)
        self.locks = defaultdict(asyncio.Lock)
        self.last_request = defaultdict(float)
        self.stealth = True
        self._stealth_attempted: set[str] = set()
        self.stealth_attempts: list[dict] = []
        self.client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=12, transport=transport,
            headers={"User-Agent": "CompanyCareersScraper/0.1 (public job listings)",
                     "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
        if output:
            (output / "responses").mkdir(parents=True, exist_ok=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlsplit(url).netloc
        last = None
        for attempt in range(self.retries + 1):
            try:
                async with self.sem:
                    async with self.locks[host]:
                        wait = self.delay - (time.monotonic() - self.last_request[host])
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self.last_request[host] = time.monotonic()
                    request = self.client.build_request(method, url, **kwargs)
                    response = await self.client.send(request, stream=True)
                    try:
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > self.max_response_bytes:
                            raise FetchError(f"Response exceeds {self.max_response_bytes} bytes: {response.url}")
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                raise FetchError(f"Response exceeds {self.max_response_bytes} bytes: {response.url}")
                            chunks.append(chunk)
                        response._content = b"".join(chunks)
                    finally:
                        await response.aclose()
                self._save(response, method, kwargs.get("json"))
                resolved = await self._escalate(method, url, kwargs, response)
                if resolved is not None:
                    self._save(resolved, method, kwargs.get("json"), via="stealth")
                    return resolved
                if response.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {response.status_code}: {response.url}"
                    if attempt < self.retries:
                        await asyncio.sleep(self._retry_delay(response, attempt))
                        continue
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                raise FetchError(f"HTTP {exc.response.status_code}: {exc.request.url}") from exc
            except httpx.RequestError as exc:
                last = f"{type(exc).__name__}: {url}: {exc}"
                if attempt < self.retries:
                    await asyncio.sleep(2 ** attempt)
        raise FetchError(last or f"Request failed: {url}")

    async def _escalate(self, method, url, kwargs, response):
        """Retry a challenged response through the stealth access layer.

        One attempt per method+URL per client. The original blocked response is
        already archived; the resolved body is archived alongside it and the
        attempt is written to ``stealth-attempts.jsonl`` when an output folder
        exists. Returns ``None`` when the response is not a challenge, stealth
        is disabled, or the escalation did not settle.
        """
        if not self.stealth or not access.stealth_enabled():
            return None
        key = f"{method.upper()} {url}"
        if key in self._stealth_attempted:
            return None
        content_type = (response.headers.get("content-type") or "").lower()
        looks_html = "html" in content_type or not content_type
        kind = access.detect_challenge(
            response.text if looks_html else "", dict(response.headers)
        )
        # Only a challenge is escalated. A bare 403/429 is an ordinary error or
        # a rate limit: a browser cannot solve it and must not be spawned.
        if not kind:
            return None
        self._stealth_attempted.add(key)
        wants_json = "json" in content_type or "json" in str(kwargs.get("headers", "")).lower()
        if method.upper() == "GET" and not wants_json:
            page = await access.fetch_with_stealth(url)
            text = page.html if page else ""
            resolved_type = "text/html; charset=utf-8"
        else:
            origin = f"{httpx.URL(url).scheme}://{httpx.URL(url).host}"
            status, text, page = await access.browser_fetch(
                url, method=method, json_body=kwargs.get("json"), warm_urls=(origin,)
            )
            resolved_type = "application/json" if wants_json else "text/html; charset=utf-8"
        attempt = access.attempt_metadata(page, url=url)
        attempt.update({"method": method.upper(), "original_status": response.status_code,
                        "original_challenge": kind})
        self.stealth_attempts.append(attempt)
        if self.output:
            with (self.output / "stealth-attempts.jsonl").open("a") as handle:
                handle.write(json.dumps(attempt, default=str) + "\n")
        if page is None or not page.ok:
            return None
        request = httpx.Request(method, url)
        return httpx.Response(
            status_code=page.status or 200,
            content=(text or "").encode("utf-8"),
            headers={"content-type": resolved_type, "x-scraper-stealth": "1"},
            request=request,
        )

    @staticmethod
    def _retry_delay(response, attempt):
        value = response.headers.get("Retry-After", "")
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                seconds = 2 ** attempt
        return max(0, min(seconds, 60))

    def _save(self, response, method, body, via=None):
        if not self.output:
            return
        digest = hashlib.sha256(response.content).hexdigest()
        target = self.output / "responses" / f"{digest}.gz"
        if not target.exists():
            target.write_bytes(gzip.compress(response.content))
        record = {
            "url": str(response.request.url), "final_url": str(response.url),
            "method": method, "request_body": body,
            "status": response.status_code, "sha256": digest,
            "content_type": response.headers.get("content-type"),
            "redirects": [str(r.url) for r in response.history],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if via:
            record["via"] = via
        with (self.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    async def json(self, url: str, *, method="GET", **kwargs):
        response = await self.request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Expected JSON, received {response.headers.get('content-type')}: {response.url}") from exc

    async def get(self, url: str, **kwargs):
        return await self.request("GET", url, **kwargs)
