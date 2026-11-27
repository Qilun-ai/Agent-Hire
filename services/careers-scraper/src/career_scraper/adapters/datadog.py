"""Enumerate the search index configured by Datadog's careers frontend, then read its job pages."""
from html import unescape
import json
import math
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from .html import from_schema, job_postings
from ..http import FetchError
from ..models import text_html


class Datadog(Adapter):
    PAGE_SIZE = 250
    VERIFICATION_REVISION = "datadog.typesense-unfiltered-localized-native.1"

    @staticmethod
    def _native_detail_url(doc):
        """The link the company listing card actually renders.

        Datadog's web bundle builds card hrefs as
        ``${language === "en" ? "" : "/" + language}/detail/${job_id}/${rel_url}``.
        The Typesense document's ``absolute_url`` omits the language prefix,
        which 404s for every non-English document; the prefixed URL is the
        live native detail page. English (and unknown-language) documents
        keep the indexed absolute URL unchanged.
        """
        absolute = doc.get("absolute_url") or ""
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "careers.datadoghq.com":
            raise ValueError("Datadog indexed posting moved to another origin; verify its ownership")
        language = doc.get("language")
        job_id = doc.get("job_id")
        if language and language != "en" and job_id is not None:
            if not parsed.path.startswith(f"/{language}/"):
                rel_url = doc.get("rel_url") or f"?gh_jid={job_id}"
                if not rel_url.startswith("?"):
                    rel_url = "?" + rel_url
                return f"https://careers.datadoghq.com/{language}/detail/{job_id}/{rel_url}"
        return absolute

    async def config(self):
        page = await self.http.get(self.source.url)
        soup = BeautifulSoup(page.text, "lxml")
        script = soup.select_one('script[src*="/assets/scripts/main-"]')
        if script is None:
            raise ValueError("Datadog careers search application script missing")
        url = urljoin(str(page.url), script["src"])
        if urlsplit(url).hostname != "careers.datadoghq.com":
            raise ValueError("Datadog careers script moved; verify its new origin")
        response = await self.http.get(url)
        def value(name):
            match = re.search(name + r':("(?:[^"\\]|\\.)*")', response.text)
            if not match:
                raise ValueError(f"Datadog public search configuration missing: {name}")
            return json.loads(match.group(1))
        host, collection = value("TYPESENSE_HOST"), value("TYPESENSE_COLLECTION")
        if not re.fullmatch(r"[a-z0-9-]+", host) or not re.fullmatch(r"[a-zA-Z0-9_-]+", collection):
            raise ValueError("Datadog careers search host/index configuration changed")
        self.result.metadata["configuration_source_url"] = url
        self.result.metadata["search_collection"] = collection
        embedded = re.search(r'GH_JOB_BOARD:("(?:[^"\\]|\\.)*")', response.text)
        self.embedded_board = json.loads(embedded.group(1)) if embedded and "grnhse_app" in response.text else None
        if self.embedded_board and not re.fullmatch(r"[a-zA-Z0-9_-]+", self.embedded_board):
            self.embedded_board = None
        if self.embedded_board:
            self.result.metadata["embedded_greenhouse_board"] = self.embedded_board
        # This search-only credential is published by the website for its public jobs interface.
        return f"https://{host}.a1.typesense.net/collections/{quote(collection)}/documents/search", value("TYPESENSE_PUBLIC_KEY")

    async def run(self):
        api, public_key = await self.config()
        headers = {"X-TYPESENSE-API-KEY": public_key}

        async def enumerate_index():
            page, documents, ids = 1, [], set()
            total = None
            while self.page_guard():
                data = await self.http.json(api, headers=headers, params={
                    "q": "*", "preset": "careers_list_view", "per_page": self.PAGE_SIZE, "page": page})
                if not isinstance(data.get("found"), int) or not isinstance(data.get("hits"), list):
                    raise ValueError("Datadog careers search response schema changed")
                if data.get("search_cutoff"):
                    raise ValueError("Datadog careers search returned a truncated result")
                total = data["found"] if total is None else total
                if data["found"] != total:
                    self.result.errors.append("datadog_total_changed_during_listing")
                before = len(ids)
                for hit in data["hits"]:
                    doc = hit["document"]
                    if doc["id"] not in ids:
                        ids.add(doc["id"])
                        documents.append(doc)
                if page >= max(1, math.ceil(total / self.PAGE_SIZE)):
                    break
                if len(ids) == before:
                    raise ValueError("Datadog careers search repeated an earlier page")
                page += 1
            if total is None or len(documents) != total:
                raise ValueError(f"Datadog search count mismatch: {len(documents)}/{total}")
            return total, documents

        total, docs = await enumerate_index()
        self.result.expected_total = total
        # The company UI's default Search widget filters to `language: en`;
        # this flow deliberately enumerates every language the collection
        # holds (the UI can switch language preference to the same set), so
        # no country/age/seniority/language filter is applied.
        language_counts: dict[str, int] = {}
        for doc in docs:
            lang = doc.get("language") or "unknown"
            language_counts[lang] = language_counts.get(lang, 0) + 1
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        self.result.metadata["typesense_request_shape"] = {
            "q": "*", "preset": "careers_list_view", "per_page": self.PAGE_SIZE,
            "collection": self.result.metadata.get("search_collection"),
            "page_size_note": "Company Search widget uses hitsPerPage 10; 250 shares the "
            "same q/preset membership and was verified to return the same found count.",
        }
        self.result.metadata["native_detail_url_construction"] = (
            "Card href template from the company bundle: language prefix only when "
            "language != en, then /detail/<job_id>/<rel_url>. The indexed absolute_url "
            "is used unchanged for English documents and is never requested for "
            "non-English documents because it returns 404 there."
        )
        self.result.metadata["language_counts"] = language_counts
        for doc in docs:
            native_url = self._native_detail_url(doc)
            record = self.job(doc, title=unescape(doc["title"]), job_id=doc.get("job_id"), url=native_url,
                locations=[{"display": doc.get("location_string"), "regions": {
                    name: value for name, value in doc.items() if name.startswith("location_") and isinstance(value, list)}}],
                department=doc.get("department"), team=doc.get("team"), date_updated=doc.get("last_mod"), language=doc.get("language"))
            record.raw["native_detail_url"] = native_url
            record.raw["native_url_source"] = (
                "datadog.language_prefixed_detail" if native_url != doc.get("absolute_url")
                else "datadog.indexed_absolute_url"
            )
            self.add(record)
            if self.limit():
                break
        self.result.listing_complete = "sample_limit" not in self.result.warnings and len(self.seen) == total

        async def detail(job):
            try:
                response = await self.http.get(job.url)
            except FetchError as exc:
                if self.embedded_board:
                    return await self.embedded_detail(job, str(exc))
                raise
            schemas = job_postings(response.text)
            if len(schemas) == 1:
                full = from_schema(self.company.name, "datadog", schemas[0], str(response.url))
                full.job_id = job.job_id
                full.raw["listing"] = job.raw
                return full
            soup = BeautifulSoup(response.text, "lxml")
            containers = soup.select(".job-description")
            if len(containers) != 1:
                raise ValueError("Datadog full job-description container missing or ambiguous")
            node = containers[0]
            job.description_html = str(node)
            job.description_source_url, job.description_method = str(response.url), "datadog.job-description"
            match = re.search(r"Requisition\s+ID:\s*([A-Za-z0-9_-]+)", node.get_text(" ", strip=True))
            if match:
                job.requisition_id = match.group(1)
            for anchor in soup.select("a[href]"):
                if anchor.get_text(" ", strip=True).lower() in {"apply", "apply now", "apply for this job"}:
                    target = urljoin(str(response.url), anchor["href"])
                    if urlsplit(target).scheme in {"http", "https"}:
                        job.apply_url = target
                        break
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        if self.result.listing_complete:
            _, final = await enumerate_index()
            if {doc["id"] for doc in docs} != {doc["id"] for doc in final}:
                self.result.listing_complete = False
                self.result.errors.append("datadog_listings_changed_during_run")
        return self.result

    async def embedded_detail(self, job, native_error):
        """Recover a broken native page using its explicitly configured application board and posting ID."""
        api = f"https://boards-api.greenhouse.io/v1/boards/{self.embedded_board}/jobs/{job.job_id}"
        row = await self.http.json(api)
        if str(row.get("id")) != str(job.job_id) or not row.get("content"):
            raise ValueError("Datadog embedded board did not match the requested public posting")
        # The generic hosted board URL can redirect back to the broken native page. Verify the actual embed entry.
        response = await self.http.get("https://boards.greenhouse.io/embed/job_app", params={
            "for": self.embedded_board, "token": job.job_id})
        parsed = urlsplit(str(response.url))
        title = unescape(row["title"])
        identity, application = None, {}
        marker = re.search(r"window\.__remixContext\s*=\s*", response.text)
        if marker:
            try:
                context = json.JSONDecoder().raw_decode(response.text[marker.end():])[0]
                application = context.get("state", {}).get("loaderData", {}).get("routes/embed.job_app", {})
                if "jobPostId" in application and "urlToken" in application:
                    identity = str(application["jobPostId"]) == str(job.job_id) and application["urlToken"] == self.embedded_board
            except (ValueError, TypeError, AttributeError):
                pass
        title_matches = " ".join(title.casefold().split()) in " ".join(text_html(response.text).casefold().split())
        if parsed.hostname not in {"boards.greenhouse.io", "job-boards.greenhouse.io"} or identity is False or (identity is None and not title_matches):
            raise ValueError("Datadog embedded application did not identify the requested role")
        job.raw["native_detail_error"] = native_error
        job.raw["greenhouse_detail"] = row
        job.raw["native_detail_url"] = job.url
        if identity:
            job.raw["embedded_application_identity"] = {"jobPostId": application["jobPostId"], "urlToken": application["urlToken"]}
        job.url, job.apply_url = str(response.url), str(response.url)
        job.title = title
        job.description_html = row["content"]
        job.description_method, job.description_source_url = "datadog.embedded_greenhouse.content", api
        job.date_posted = row.get("first_published")
        job.date_updated = row.get("updated_at") or job.date_updated
        job.salary = row.get("pay_input_ranges")
        job.language = application.get("jobPost", {}).get("language") or row.get("language") or job.language
        if row.get("requisition_id"):
            job.requisition_id = str(row["requisition_id"])
        else:
            match = re.search(r"Requisition\s+ID:\s*([A-Za-z0-9_-]+)", text_html(row["content"]))
            if match:
                job.requisition_id = match.group(1)
        job.warnings.append("native_page_recovered_via_embedded_board")
        self.result.metadata.setdefault("recovered_native_pages", []).append(job.raw["native_detail_url"])
        return job
