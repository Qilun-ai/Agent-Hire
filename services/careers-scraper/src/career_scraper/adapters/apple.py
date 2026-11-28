"""Apple's worldwide native search and exact, location-specific native jobs.

The listing's public ID is authoritative. In particular, a shared positionId
does not make two location postings the same record. No ATS is used.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from html import escape
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


ENTRY = "https://jobs.apple.com/en-us/search"
SECTIONS = (
    ("jobSummary", "Summary"),
    ("description", "Description"),
    ("responsibilities", "Responsibilities"),
    ("minimumQualifications", "Minimum Qualifications"),
    ("keyQualifications", "Key Qualifications"),
    ("preferredQualifications", "Preferred Qualifications"),
    ("educationAndExperience", "Education & Experience"),
    ("additionalRequirements", "Additional Requirements"),
)
# These are application/account state, rather than public vacancy fields.
PRIVATE_FIELDS = {
    "internalDetails",
    "internalInformation",
    "isInternalJobDetails",
    "isInternal",
    "selfDisclosureConfig",
    "questionarieTemplateID",
    "questionCategories",
    "isFavorited",
    "submitted",
    "resubmitted",
    "talentLimitDetails",
}


def apple_state(html):
    marker = "window.__staticRouterHydrationData = JSON.parse("
    start = html.index(marker) + len(marker)
    encoded = json.JSONDecoder().raw_decode(html[start:].lstrip())[0]
    return json.loads(encoded)["loaderData"]


def native_url(row):
    identity = row["positionId"] if row["id"].startswith("PIPE-") else row["id"]
    return f"https://jobs.apple.com/en-us/details/{identity}/{row['transformedPostingTitle']}"


def canonical_text(value):
    return re.sub(r"[\s\ufeff]", "", BeautifulSoup(value or "", "lxml").get_text())


def public_info(info):
    """Preserve every vacancy field and locale while removing account state."""
    result = {k: deepcopy(v) for k, v in info.items() if k not in PRIVATE_FIELDS}
    for local in result.get("localizations", {}).values():
        if isinstance(local.get("posting"), dict):
            local["posting"] = {
                k: v for k, v in local["posting"].items() if k not in PRIVATE_FIELDS
            }
    return result


def stable_listing(row):
    result = deepcopy(row)
    # The observed client POST uses URL locale en-us. Its public response has
    # the same teamID/teamCode but a null translated teamName; SSR uses en_US
    # and supplies that name. Preserve both raw fields, compare stable IDs.
    if isinstance(result.get("team"), dict):
        result["team"].pop("teamName", None)
    if isinstance(result.get("localeInfo"), dict):
        language = result["localeInfo"].get("languageCode")
        if language in ("en_US", "en-us"):
            result["localeInfo"]["languageCode"] = "en_US"
    for location in result.get("locations", []):
        # The same postLocation-NYC is labelled New York City by SSR and
        # New York by the native client. Preserve raw labels; compare its ID.
        location.pop("name", None)
    # Managed evergreen PIPE rows are assigned the request time on every search.
    # The dated per-location posting record is preserved separately in jobsData.
    if row.get("managedPipelineRole"):
        result.pop("postDateInGMT", None)
        result.pop("postingDate", None)
    return result


def row_signature(row):
    return sha256(
        json.dumps(stable_listing(row), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def selected_location(row, info):
    if str(info.get("positionId")) != str(row["positionId"]):
        raise ValueError("Apple native detail belongs to a different base position")
    if info.get("postingTitle") != row["postingTitle"]:
        raise ValueError("Apple native detail/listing title mismatch")
    listed = {v["postLocationId"] for v in row["locations"]}
    locations = [v for v in info.get("locations", []) if v.get("id") in listed]
    if len(locations) != len(listed) or not locations:
        raise ValueError(
            "Apple native detail does not expose the exact listed location"
        )
    if not row["id"].startswith("PIPE-") and "-" in row["id"]:
        suffix = row["id"].split("-", 1)[1]
        if len(locations) != 1 or locations[0].get("postingIdentifier") != suffix:
            raise ValueError("Apple location suffix and postingIdentifier disagree")
    return locations


def html_section(value):
    return (
        value
        if re.search(
            r"</?(?:p|br|div|span|ul|ol|li|a|strong|em|b|i)(?:\s|/?>)", value, re.I
        )
        else "<p>" + escape(value).replace("\n", "<br>") + "</p>"
    )


def sections_for(row, info):
    locations = selected_location(row, info)
    locale = info.get("selectedLocale", "en_US")
    sections = [
        {"field": key, "label": label, "content": info[key]}
        for key, label in SECTIONS
        if isinstance(info.get(key), str) and info[key].strip()
    ]
    # A location-specific posting shows precisely its own locale's footer.
    # Other locations/locales remain intact in raw.detail.
    selected = {v["id"] for v in locations}
    for footer in info.get("postingFooters", []):
        if footer.get("postLocationId") not in selected:
            continue
        blocks = footer.get("localizations", {}).get(locale, [])
        for block in sorted(blocks, key=lambda v: v.get("displayOrder", 0)):
            if block.get("content"):
                sections.append(
                    {
                        "field": "postingFooters",
                        "location_id": footer["postLocationId"],
                        "label": block.get("label") or "",
                        "name": block.get("name"),
                        "content": block["content"],
                        "displayOrder": block.get("displayOrder"),
                    }
                )
    return sections


class Apple(Adapter):
    VERIFICATION_REVISION = "apple.worldwide-exact-location-native-full-fields.1"
    PAGE_SIZE = 20

    def validate_row(self, row):
        if (
            not isinstance(row, dict)
            or not re.fullmatch(r"(?:PIPE-)?\d+(?:-\d+)?", str(row.get("id", "")))
            or not re.fullmatch(r"\d+", str(row.get("positionId", "")))
            or not row.get("postingTitle")
            or not row.get("transformedPostingTitle")
            or not isinstance(row.get("locations"), list)
            or not row["locations"]
        ):
            raise ValueError("Apple native listing identity or locations changed")
        base_identity = row["id"].removeprefix("PIPE-").split("-", 1)[0]
        if base_identity != str(row["positionId"]):
            raise ValueError("Apple exact listing ID and shared base position disagree")

    async def snapshot(self, phase, saved_pages=None):
        started = time.perf_counter()
        if not getattr(self, "_entry_checked", False):
            entry = await self.http.get(ENTRY, params={"location": ""})
            bootstrap = apple_state(entry.text)["search"]
            if bootstrap.get("filters") or bootstrap.get("search"):
                raise ValueError(
                    "Apple native search entry applied an unexpected filter"
                )
            self.result.metadata["native_search_bootstrap"] = {
                "url": str(entry.url),
                "total": bootstrap.get("totalRecords"),
                "pagination": "POST https://jobs.apple.com/api/v1/search;20 rows;newest;no filters",
                "upstream_missing_fields": "Client POST returns null teamName; public native detail retains teamNames. Raw null is preserved.",
            }
            self._entry_checked = True
        token_response = await self.http.get("https://jobs.apple.com/api/v1/CSRFToken")
        token = token_response.headers.get("X-Apple-CSRF-Token")

        async def page(number):
            nonlocal token
            if not self.page_guard():
                raise ValueError("Apple native listing page budget exhausted")
            headers = {
                "locale": "en-us",
                "browserLocale": "en-us",
                "Origin": "https://jobs.apple.com",
                "Referer": ENTRY + "?location=",
            }
            if token:
                headers["X-Apple-CSRF-Token"] = token
            response = await self.http.request(
                "POST",
                "https://jobs.apple.com/api/v1/search",
                headers=headers,
                json={
                    "query": "",
                    "filters": {},
                    "page": number,
                    "locale": "en-us",
                    "sort": "newest",
                    "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
                },
            )
            token = response.headers.get("X-Apple-CSRF-Token") or token
            state = response.json().get("res")
            if not isinstance(state, dict) or not isinstance(
                state.get("searchResults"), list
            ):
                raise ValueError(
                    "Apple native client pagination response schema changed"
                )
            return state

        saved_pages = saved_pages or {}
        first = saved_pages.get(1) or await page(1)
        total = first.get("totalRecords")
        if not isinstance(total, int) or total < 0:
            raise ValueError("Apple native worldwide listing count is missing")
        count = max(1, math.ceil(total / self.PAGE_SIZE))
        if total == 0 and self.result.metadata["native_search_bootstrap"].get("total"):
            raise ValueError(
                "Apple native client returned zero while its official entry lists jobs"
            )
        # Match the client request and rotate its public anti-CSRF token in
        # response order. Cached SSR GET pages demonstrably overlap at boundaries.
        states = [first]
        for number in range(2, count + 1):
            states.append(saved_pages.get(number) or await page(number))
        reported = [state.get("totalRecords") for state in states]
        if any(not isinstance(value, int) or value < 0 for value in reported):
            raise ValueError("Apple native listing count changed schema")
        # A new posting may add one final page while pagination is in flight.
        # Retain the first capture, fetch only newly exposed trailing pages and
        # the leading insertion region, then require exact final membership.
        final_total = reported[-1]
        final_count = max(1, math.ceil(final_total / self.PAGE_SIZE))
        for number in range(count + 1, final_count + 1):
            state = await page(number)
            states.append(state)
            reported.append(state["totalRecords"])
        total = reported[-1]
        count = len(states)
        changed_count = len(set(reported)) > 1
        if any(after < before for before, after in zip(reported, reported[1:])):
            # A transient downward count during a long sweep is recorded as
            # evidence in listing_snapshots; it is not by itself a membership
            # change. Certification still requires identical initial/final IDs.
            self.result.warnings.append(f"apple_{phase}_count_decreased_during_capture")
        rows, pages = [], []
        for number, state in enumerate(states, 1):
            batch = state.get("searchResults")
            if not isinstance(batch, list):
                raise ValueError("Apple native posting rows changed schema")
            expected = min(
                self.PAGE_SIZE,
                max(0, state["totalRecords"] - (number - 1) * self.PAGE_SIZE),
            )
            if len(batch) != expected:
                raise ValueError(
                    f"Apple native page {number} returned an unexpected number of postings"
                )
            for row in batch:
                self.validate_row(row)
            rows.extend(batch)
            pages.append(
                {
                    "page": number,
                    "count": len(batch),
                    "reported_total": state["totalRecords"],
                    "ids": [r["id"] for r in batch],
                }
            )
        ids = [row["id"] for row in rows]
        resolved = {row["id"]: row for row in rows}
        repair_pages = []
        if len(resolved) != total or changed_count:
            counts = {}
            for identity in ids:
                counts[identity] = counts.get(identity, 0) + 1
            repeated = {identity for identity, size in counts.items() if size > 1}
            boundaries = {
                index // self.PAGE_SIZE + 1
                for index, identity in enumerate(ids)
                if identity in repeated
            }
            # Managed national PIPE rows receive fresh timestamps on each
            # request. A tie can shift across its page boundary. Revisit only
            # the affected native pages and immediate neighbours, once.
            repair_numbers = sorted(
                {
                    n
                    for number in boundaries
                    for n in (number - 1, number, number + 1)
                    if 1 <= n <= count
                }
            )
            if changed_count:
                # Managed country-wide roles sort before dated vacancies. New
                # vacancies insert immediately after them. Capture that leading
                # region again; the original rows and every repair stay visible.
                managed_prefix = next(
                    (
                        i
                        for i, row in enumerate(rows)
                        if not row.get("managedPipelineRole")
                    ),
                    len(rows),
                )
                prefix_pages = min(
                    count, math.ceil(managed_prefix / self.PAGE_SIZE) + 2
                )
                repair_numbers = sorted(
                    set(repair_numbers) | set(range(1, prefix_pages + 1))
                )
            for number in repair_numbers:
                state = await page(number)
                batch = state["searchResults"]
                expected = min(
                    self.PAGE_SIZE, max(0, total - (number - 1) * self.PAGE_SIZE)
                )
                if state.get("totalRecords") != total or len(batch) != expected:
                    raise ValueError(
                        "Apple native count changed during bounded boundary repair"
                    )
                for row in batch:
                    self.validate_row(row)
                    resolved[row["id"]] = row
                repair_pages.append(
                    {
                        "page": number,
                        "count": len(batch),
                        "ids": [r["id"] for r in batch],
                    }
                )
            if len(resolved) != total:
                self.result.errors.append(
                    f"apple_{phase}_duplicate_or_missing_listing_ids"
                )
        self.result.metadata.setdefault("listing_snapshots", []).append(
            {
                "phase": phase,
                "total": total,
                "initial_total": reported[0],
                "count_changed_during_capture": changed_count,
                "saved_pages_reused": len(saved_pages),
                "distinct_ids_before_repair": len(set(ids)),
                "distinct_ids": len(resolved),
                "pages": pages,
                "repair_pages": repair_pages,
                "resolved_ids": list(resolved),
            }
        )
        self.result.timings[f"{phase}_listing_seconds"] = time.perf_counter() - started
        return list(resolved.values())

    def full_job(
        self,
        row,
        info,
        *,
        evidence_url,
        fetched_at=None,
        provenance=None,
        native_meta_links=None,
    ):
        locations = selected_location(row, info)
        if (
            not isinstance(info.get("description"), str)
            or not info["description"].strip()
        ):
            raise ValueError("Apple native full description is missing")
        if canonical_text(info.get("jobSummary")) != canonical_text(
            row.get("jobSummary")
        ):
            raise ValueError("Apple native detail and listed full summary disagree")
        sections = sections_for(row, info)
        body = "\n".join(
            (f"<h2>{escape(s['label'])}</h2>" if s["label"] else "")
            + html_section(s["content"])
            for s in sections
        )
        locale = info.get("selectedLocale", "en_US")
        date_map = info.get("postingPostLocationData", {}).get(locale, {})
        dates = {
            v["id"]: date_map.get(v["id"], {}).get("postingDate") for v in locations
        }
        actual = sorted({value for value in dates.values() if value})
        # Multiple selected locations with distinct dates cannot be represented by
        # one fabricated timestamp. Preserve every dated source value in raw.
        posted = actual[0] if len(actual) == 1 else None
        salary = [s for s in sections if s.get("name") == "Pay & Benefits"]
        apply = (
            native_url(row) + "/locationPicker"
            if row.get("managedPipelineRole")
            else f"https://jobs.apple.com/app/en-us/apply/{row['id']}"
        )
        raw = {
            "listing": deepcopy(row),
            "detail": public_info(info),
            "rendered_sections": sections,
            "selected_locations": locations,
            "actual_location_posting_dates": dates,
            "listing_signature": row_signature(row),
            "detail_provenance": provenance or {},
            "verification_revision": self.VERIFICATION_REVISION,
            "native_meta_links": deepcopy(native_meta_links or []),
            "detail_apply_url": (
                native_url(row) + "/locationPicker"
                if info.get("managedPipelineRole")
                else f"https://jobs.apple.com/app/en-us/apply/{info['id']}"
            ),
        }
        links = [
            urljoin(evidence_url, a["href"])
            for a in BeautifulSoup(body, "lxml").select("a[href]")
        ]
        attachments = sorted(
            {
                url
                for url in links
                if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", url, re.I)
            }
        )
        raw["published_links"] = list(dict.fromkeys(links))
        raw["published_attachment_urls"] = attachments
        job = self.job(
            raw,
            title=row["postingTitle"],
            job_id=row["id"],
            requisition_id=row.get("reqId"),
            url=native_url(row),
            apply_url=apply,
            description=body,
            method="apple.native_exact_location.full_sections",
            description_url=evidence_url,
            locations=locations,
            team=row.get("team"),
            department=info.get("teamNames"),
            employment_type=info.get("employmentType"),
            workplace_type="Home office" if info.get("homeOffice") else None,
            date_posted=posted,
            date_posted_raw={
                "location_dates": dates,
                "listing_date": row.get("postingDate"),
                "detail_display_date": info.get("postingDate"),
            },
            qualifications={
                key: info.get(key)
                for key in (
                    "minimumQualifications",
                    "keyQualifications",
                    "preferredQualifications",
                )
            },
            responsibilities=info.get("responsibilities"),
            education=info.get("educationAndExperience"),
            salary=salary or None,
            employer="Apple",
            language=locale,
        )
        if fetched_at:
            job.fetched_at = fetched_at
        # The EEO rights notice is an explicitly labelled public policy reference,
        # not an attached vacancy description. Unknown documents require review.
        role_documents = [
            url for url in attachments if urlsplit(url).hostname != "www.eeoc.gov"
        ]
        if role_documents:
            job.warnings.append("description_not_full_field")
            raw["unresolved_role_attachment_urls"] = role_documents
        return job

    async def fetch_detail(self, job):
        response = await self.http.get(job.url)
        if urlsplit(str(response.url)).path != urlsplit(job.url).path:
            raise ValueError(
                "Apple exact native detail redirected away from the listed posting"
            )
        detail_state = apple_state(response.text).get("jobDetails", {})
        info = detail_state.get("jobsData")
        if not info:
            raise ValueError("Apple exact listed posting returned no public jobsData")
        return self.full_job(
            job.raw["listing"],
            info,
            evidence_url=str(response.url),
            native_meta_links=detail_state.get("metaLinks", []),
        )

    async def collect(self, rows):
        # A cache from the legacy adapter may have omitted Responsibilities or
        # collapsed a location. Reuse only this full-field revision and current
        # exact listing identity/signature, even if its old boolean was true.
        current = {native_url(row): row for row in rows}
        self.options.cached_jobs = {
            url: cached
            for url, cached in self.options.cached_jobs.items()
            if url in current
            and cached.raw.get("verification_revision") == self.VERIFICATION_REVISION
            and cached.raw.get("listing_signature") == row_signature(current[url])
            and cached.job_id == current[url]["id"]
        }
        snapshots = self.result.metadata.get("listing_snapshots", [])
        self.result.expected_total = snapshots[0]["total"] if snapshots else len(rows)
        for row in rows:
            self.add(
                self.job(
                    {"listing": row},
                    title=row["postingTitle"],
                    job_id=row["id"],
                    requisition_id=row.get("reqId"),
                    url=native_url(row),
                    locations=row["locations"],
                )
            )
            if self.limit():
                break
        if "sample_limit" not in self.result.warnings:
            final = await self.snapshot("final")
            before = {r["id"]: row_signature(r) for r in rows}
            after = {r["id"]: row_signature(r) for r in final}
            self.result.metadata["membership_reconciliation"] = {
                "added": sorted(after.keys() - before.keys()),
                "removed": sorted(before.keys() - after.keys()),
                "changed": sorted(
                    k for k in before.keys() & after.keys() if before[k] != after[k]
                ),
            }
            if before != after:
                self.result.errors.append(
                    "apple_native_membership_or_fields_changed_during_collection"
                )
            self.result.listing_complete = (
                before == after and len(before) == len(rows) and not self.result.errors
            )
        self.result.jobs = await self.hydrate(self.result.jobs, self.fetch_detail)
        return self.result

    async def run(self):
        return await self.collect(await self.snapshot("initial"))
