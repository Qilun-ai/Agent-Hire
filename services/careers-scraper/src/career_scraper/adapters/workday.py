import re
import json
from hashlib import sha256
from urllib.parse import urlsplit

from .base import Adapter


def coordinates(url):
    parsed = urlsplit(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts.pop(0)
    if not parts:
        raise ValueError("Workday needs a career site path, not only the tenant hostname")
    tenant = parsed.hostname.split(".")[0]
    site = parts[0]
    if parts[:2] == ["wday", "cxs"]:
        tenant, site = parts[2:4]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin, tenant, site


class Workday(Adapter):
    async def run(self):
        origin, tenant, site = coordinates(self.source.url)
        base = f"{origin}/wday/cxs/{tenant}/{site}"
        visited_slices = set()
        slice_count = 0

        async def crawl(facets, depth=0):
            nonlocal slice_count
            signature = repr(sorted(facets.items()))
            if signature in visited_slices or slice_count >= 500:
                self.result.errors.append("workday_facet_budget_exhausted")
                return False
            visited_slices.add(signature)
            slice_count += 1
            offset, local_ids = 0, set()
            first = None
            while self.page_guard():
                payload = {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""}
                data = await self.http.json(base + "/jobs", method="POST", json=payload)
                if first is None:
                    first = data
                    if not facets:
                        self.result.expected_total = int(data["total"])
                # CXS commonly returns total=0 after the first page even while returning jobs.
                # The first response owns the slice total; later zero values are not exhaustion.
                total = int(first["total"])
                if offset and data.get("total", 0) not in (0, total):
                    self.result.errors.append(f"workday_total_changed: {total} -> {data['total']}")
                rows = data["jobPostings"]
                previous = len(local_ids)
                for row in rows:
                    if not row.get("externalPath"):
                        original = row
                        recovered = None
                        req_id = next((x for x in row.get("bulletFields", [])
                                       if isinstance(x, str) and re.fullmatch(r"[A-Za-z]*\d[\w-]*", x)), None)
                        if req_id and self.page_guard():
                            try:
                                repair = await self.http.json(base + "/jobs", method="POST", json={
                                    "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": req_id})
                                recovered = next((r for r in repair.get("jobPostings", [])
                                    if req_id in r.get("bulletFields", []) and r.get("externalPath")), None)
                            except Exception as exc:
                                self.result.errors.append(f"workday_metadata_recovery_failed: {req_id}: {exc}")
                        if recovered:
                            row = {**recovered, "listing_metadata_recovered_from": original}
                            self.result.warnings.append("workday_listing_metadata_recovered")
                        else:
                            marker = sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
                            local_ids.add("missing:" + marker)
                            missing = self.result.metadata.setdefault("unresolved_listing_records", [])
                            if not any(m["record"] == original for m in missing):
                                missing.append({"record": original, "offset": offset, "facets": facets})
                                self.result.errors.append(f"workday_listing_metadata_missing: {req_id or marker[:12]}")
                            continue
                    path = row["externalPath"]
                    local_ids.add(path)
                    self.add(self.job(row, title=row.get("title"), job_id=path, url=f"{origin}/{site}{path}",
                        locations=[row.get("locationsText")], date_posted_raw=row.get("postedOn")))
                    if self.limit():
                        return False
                # Some tenants clamp total AND offsets at 2,000. Split using facets.
                if total >= 2000 and (offset + len(rows) >= 2000 or len(local_ids) == previous):
                    candidates = [f for f in first.get("facets", [])
                                  if f.get("facetParameter") not in facets and f.get("values")]
                    # A fully counted partition must cover the advertised/clamped slice.
                    candidates = [f for f in candidates if all("count" in v for v in f["values"])
                                  and sum(v["count"] for v in f["values"]) >= total]
                    candidates.sort(key=lambda f: max(v["count"] for v in f["values"]))
                    if not candidates or depth >= 6:
                        self.result.errors.append(f"workday_2000_cap_unresolved: {facets}")
                        return False
                    facet = candidates[0]
                    self.result.warnings.append("workday_facet_partition_used")
                    statuses = []
                    for value in facet["values"]:
                        statuses.append(await crawl({**facets, facet["facetParameter"]: [value["id"]]}, depth + 1))
                        if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
                            return False
                    return all(statuses)
                if len(local_ids) >= total:
                    return True
                if not rows or len(local_ids) == previous:
                    self.result.errors.append(f"workday_pagination_incomplete: {offset}/{total}")
                    return False
                offset += len(rows)
            return False

        try:
            self.result.listing_complete = await crawl({})
        except Exception as exc:
            self.result.errors.append(f"workday_listing_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        if "workday_facet_partition_used" in self.result.warnings:
            self.result.warnings.append("expected_total_is_capped_unfiltered_count")

        async def detail(job):
            api = base + job.raw["externalPath"]
            row = await self.http.json(api)
            info = row["jobPostingInfo"]
            job.raw["detail"] = row
            job.title = info.get("title") or job.title
            job.description_html = info.get("jobDescription", "")
            job.description_method, job.description_source_url = "workday.jobPostingInfo.jobDescription", api
            job.requisition_id = info.get("jobReqId")
            job.job_id = str(info.get("jobReqId") or job.job_id)
            job.apply_url = info.get("externalUrl") or job.url
            job.date_posted = info.get("startDate")
            job.date_posted_raw = info.get("postedOn", job.date_posted_raw)
            job.valid_through = info.get("endDate")
            job.locations = [info.get("location")] + (info.get("additionalLocations") or [])
            job.employment_type = info.get("timeType")
            job.workplace_type = info.get("remoteType")
            job.employer = info.get("hiringOrganization") or info.get("company")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        return self.result
