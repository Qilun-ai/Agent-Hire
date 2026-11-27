"""Applied Materials' published native global jobs and two contractor journeys.

Membership comes exclusively from the company's own PCSX Jobs view and the
contractor directory linked by its careers page. No standalone ATS is searched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import asyncio
import re
import time

from urllib.parse import unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..models import now, text_html

ENTRY = "https://www.appliedmaterials.com/us/en/careers.html"
NATIVE = "https://careers.appliedmaterials.com"
CTA = NATIVE + "/careers?Country=United+States+of+America&domain=appliedmaterials.com&sort_by=relevance&triggerGoButton=false"
DOMAIN = "appliedmaterials.com"
CONTRACT = "https://appliedcontract.jobs"
US = "https://apply-applied-materials.opptly.ai"
US_API = "https://76pi7xyxb9.execute-api.us-east-2.amazonaws.com/api"
SEARCH = NATIVE + "/api/pcsx/search"
FILTER_DIMS = ("country", "department", "employee_type", "seniority")
PARTITION_SPLIT = 50
API_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NATIVE + "/careers",
    "Origin": NATIVE,
}


def native_search_params(filters=None, start=0, sort="hot"):
    """Build the exact unfiltered or published-facet PCSX search request."""
    params = [("domain", DOMAIN), ("query", ""), ("location", ""),
              ("start", str(start)), ("sort_by", sort)]
    for key, value in (filters or {}).items():
        params.append(("filter_" + key, value))
    return params


def native_applied_filters(value):
    """Normalize the publisher's camelCase filter echo for exact comparison."""
    mapping = {"employeeType": "employee_type", "timeType": "time_type"}
    return {mapping.get(key, key): [unquote(item) for item in items]
            for key, items in (value or {}).items()}


def epoch(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Applied Materials postedTs must be numeric or missing")
    if isinstance(value, (float, int)) and value == 0:
        return None
    if not isinstance(value, (float, int)) or value < 0:
        raise ValueError("Applied Materials postedTs must be a positive epoch timestamp")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def compact(value):
    return re.sub(r"[\s\ufeff]", "", text_html(value))


def equivalent_compact(value):
    """Compare visible job copy while allowing equivalent dash glyphs."""
    dash_map = str.maketrans({character: "-" for character in "‐‑‒–—−﹣－"})
    return compact(value).translate(dash_map)


def native_config(html):
    node = BeautifulSoup(html, "lxml").select_one("code#pcsx-data")
    value = json.loads(node.get_text()) if node else {}
    cfg = value.get("configs", {})
    pcs = cfg.get("pcsxConfig", {})
    if (value.get("domain") != DOMAIN or pcs.get("branding", {}).get("companyName") != "Applied Materials"
            or pcs.get("searchConfig", {}).get("basePositionFq") != "position.type:ats AND is_externally_posted:1 AND position.is_confidential:false AND position.ats_status:OPEN"
            or pcs.get("searchConfig", {}).get("membershipFq")
            or pcs.get("positionDetailsConfig", {}).get("tabsDisplayConfig") != [
                {"id": "jd", "sections": ["position_fields", "jd", "position_custom_content"],
                 "label": "Job description", "showInsightsWidget": True}]):
        raise ValueError("Applied Materials branded global listing or complete-body layout changed")
    return {"searchConfig": pcs["searchConfig"], "positionDetailsConfig": pcs["positionDetailsConfig"]}


def native_row(row):
    if (not isinstance(row, dict) or not isinstance(row.get("id"), int)
            or not re.fullmatch(r"R\d+", row.get("displayJobId", ""))
            or row.get("atsJobId") != row["displayJobId"] or not row.get("name")
            or row.get("positionUrl") != f"/careers/job/{row['id']}"):
        raise ValueError("Applied Materials native posting identity changed")
    epoch(row.get("postedTs"))
    return row


def native_body(row):
    body = row.get("jobDescription")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Applied Materials exact native full description missing")
    if row.get("positionExtraDetails", {}).get("customContent"):
        raise ValueError("Applied Materials published custom role content needs explicit full-section handling")
    # Standard company material alone is not a usable role description.
    plain = text_html(body)
    if "Who We Are" in plain and "Additional Information" in plain:
        tail = plain.split("Additional Information", 1)[0]
        anchor = BeautifulSoup(body, "lxml").select_one('a[href="https://hrportal.ehr.com/applied/"]')
        if anchor:
            paragraph = anchor.find_parent("p")
            prefix = text_html(str(paragraph))
            if prefix in tail:
                tail = tail.split(prefix, 1)[-1]
        if len(tail.strip()) < 100:
            raise ValueError("Applied Materials exact description contains company template without role details")
    return body


def labeled_fields(body):
    """Retain published labels; do not reinterpret creation/contract timestamps."""
    lines = text_html(body).splitlines()
    fields = {}
    for index, line in enumerate(lines[:-1]):
        if line.rstrip(":") in ("Salary", "Time Type", "Employee Type", "Travel", "Relocation Eligible"):
            fields[line.rstrip(":")] = lines[index + 1]
    return fields


def native_salary(labels):
    if "Salary" not in labels:
        return None
    published = labels["Salary"]
    amounts = re.findall(r"\d+(?:[.,]\d+)*", published)
    placeholder = bool(amounts) and all(not re.search(r"[1-9]", a) for a in amounts)
    return {"published_text": published, "source": "jobDescription", "is_zero_placeholder": placeholder}


def fwp_config(html):
    soup = BeautifulSoup(html, "lxml")
    scripts = [n.get_text() for n in soup.select("script") if "window.FWP_JSON = " in n.get_text()]
    if len(scripts) != 1:
        raise ValueError("Applied Materials Singapore contractor published pager missing")
    config, _ = json.JSONDecoder().raw_decode(scripts[0].split("window.FWP_JSON = ", 1)[1])
    if config.get("ajaxurl") != CONTRACT + "/wp-json/facetwp/v1/refresh":
        raise ValueError("Applied Materials contractor pagination endpoint changed")
    return config


def sg_links(html):
    soup = BeautifulSoup(html, "lxml")
    links = {}
    for node in soup.select('a[href]'):
        match = re.fullmatch(r"https://appliedcontract\.jobs/openings/(\d+)-[^/?#]+/", node["href"])
        if match:
            links[match[1]] = node["href"]
    return links


def sg_fields(html, identity, url):
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for node in soup.select('script[type="application/ld+json"]'):
        data = json.loads(node.get_text())
        postings += [row for row in data.get("@graph", [data]) if row.get("@type") == "JobPosting"]
    body = soup.select_one(".elementor-widget-theme-post-content")
    title = soup.select_one("h1")
    if body:
        for nav in body.select("nav.matador-job-navigation"):
            nav.decompose()
    if (len(postings) != 1 or body is None or title is None
            or equivalent_compact(postings[0].get("title", "")) != equivalent_compact(title.get_text(" ", strip=True))
            or postings[0].get("mainEntityOfPage", {}).get("@id") != url
            or not urlsplit(url).path.startswith(f"/openings/{identity}-")
            or equivalent_compact(postings[0].get("description", "")) != equivalent_compact(str(body))):
        raise ValueError("Applied Materials contractor exact identity/schema/full native body differs")
    return postings[0], str(body), title.get_text(" ", strip=True)


class AppliedMaterials(Adapter):
    VERIFICATION_REVISION = "applied-materials.official-native-global-and-raise-contractors.2"
    PAGE_SIZE = 10

    async def snapshot(self, phase):
        """Enumerate every published native row through the site's own filter facets.

        The publisher's company-priority ordering can reorder between offset
        requests, so offset pagination alone cannot certify membership. The
        published facets (country, department, employee type, seniority) split
        the same unfiltered result set into smaller exact queries; every leaf is
        still requested from the company's own /api/pcsx/search endpoint with
        sort_by=hot. Leaves whose facet counts overlap are kept whole and
        paginated, and a boundary duplicate is repaired by a narrower facet.
        """
        started = time.perf_counter()
        requests = 0

        async def search(filters=None, start=0, sort="hot"):
            nonlocal requests
            if self.result.pages + requests >= self.options.max_pages:
                raise ValueError("Applied Materials native partition budget exhausted")
            filters = filters or {}
            response = await self.http.json(SEARCH, params=native_search_params(filters, start, sort), headers=API_HEADERS)
            requests += 1
            data = response.get("data", {}) if isinstance(response, dict) else {}
            count = data.get("count")
            batch = data.get("positions")
            if (response.get("status") != 200 or not isinstance(count, int) or isinstance(count, bool)
                    or count < 0 or not isinstance(batch, list) or data.get("sortBy") != sort
                    or native_applied_filters(data.get("appliedFilters")) != {k: [v] for k, v in filters.items()}):
                raise ValueError("Applied Materials native filtered listing changed")
            expected = min(self.PAGE_SIZE, max(0, count - start))
            if len(batch) != expected:
                raise ValueError("Applied Materials native filtered page row count changed")
            ids = []
            for row in batch:
                native_row(row)
                identity = str(row["id"])
                if identity in ids:
                    raise ValueError(f"Applied Materials native filtered page repeats {identity}")
                ids.append(identity)
            return data, ids

        root, root_ids = await search()
        total = root["count"]
        if len(root_ids) != min(self.PAGE_SIZE, total):
            raise ValueError("Applied Materials native first page incomplete")
        options = {}
        for item in (root.get("filterDef") or {}).get("allFilters") or []:
            options[item.get("filterName")] = [option["value"] for option in item.get("options") or []]
        for dimension in FILTER_DIMS:
            if not options.get(dimension):
                raise ValueError(f"Applied Materials published filter {dimension} missing")

        leaves = []

        async def split(filters, data, dimensions):
            if data["count"] <= PARTITION_SPLIT or not dimensions:
                leaves.append({"filters": filters, "count": data["count"], "first": data, "dimensions": dimensions})
                return
            dimension = dimensions[0]
            values = options.get(dimension) or []
            children = await asyncio.gather(*(search({**filters, dimension: value}) for value in values))
            if sum(child[0]["count"] for child in children) != data["count"]:
                # A facet that overlaps or omits rows cannot partition; keep the
                # parent whole and paginate it with offset evidence.
                await split(filters, data, dimensions[1:])
                return
            for value, (child, _) in zip(values, children):
                await split({**filters, dimension: value}, child, dimensions[1:])

        await split({}, root, FILTER_DIMS)

        async def fetch_rows(filters, first, sort="hot"):
            count = first["count"]
            pages = {0: first["positions"]}
            starts = list(range(self.PAGE_SIZE, count, self.PAGE_SIZE))
            if starts:
                extra = await asyncio.gather(*(search(filters, start, sort) for start in starts))
                for start, (page, _) in zip(starts, extra):
                    if page["count"] != count:
                        raise ValueError("Applied Materials native partition count changed during pagination")
                    pages[start] = page["positions"]
            rows = {}
            for start in sorted(pages):
                for row in pages[start]:
                    rows.setdefault(str(row["id"]), row)
            return rows

        async def repair(filters, count, dimensions):
            # A repeated row at a page boundary can hide one exact posting.
            # Re-partition the same parent on a narrower published facet; the
            # union must still cover every row of the parent.
            for dimension in dimensions:
                values = options.get(dimension) or []
                if not values:
                    continue
                children = await asyncio.gather(*(search({**filters, dimension: value}) for value in values))
                if sum(child[0]["count"] for child in children) < count:
                    continue
                rows = {}
                for value, (child, _) in zip(values, children):
                    if child["count"]:
                        child_rows = await fetch_rows({**filters, dimension: value}, child)
                        for identity, row in child_rows.items():
                            rows.setdefault(identity, row)
                if len(rows) == count:
                    return rows
            return None

        async def fetch_leaf(leaf):
            filters, first = leaf["filters"], leaf["first"]
            count = first["count"]
            rows = await fetch_rows(filters, first)
            fallback = None
            if len(rows) != count:
                # Company-priority pages can repeat a boundary row. The site's
                # own relevance ordering has different boundaries; the union of
                # both published orders recovers every exact row.
                relevance_first, _ = await search(filters, 0, "relevance")
                relevance_rows = await fetch_rows(filters, relevance_first, "relevance")
                union = dict(rows)
                for identity, row in relevance_rows.items():
                    union.setdefault(identity, row)
                if len(union) == count:
                    rows = union
                    fallback = "relevance"
            if len(rows) != count:
                repaired = await repair(filters, count, leaf["dimensions"])
                if repaired is not None:
                    rows = repaired
                    fallback = (fallback + "+facet") if fallback else "facet"
            if len(rows) != count:
                raise ValueError(f"Applied Materials native partition {filters} has {len(rows)} of {count} rows")
            return rows, fallback

        results = await asyncio.gather(*(fetch_leaf(leaf) for leaf in leaves))
        rows = {}
        duplicate_ids = []
        leaf_metadata = []
        for leaf, (leaf_rows, fallback) in zip(leaves, results):
            for identity, row in leaf_rows.items():
                if identity in rows:
                    duplicate_ids.append(identity)
                rows.setdefault(identity, row)
            leaf_metadata.append({"filters": leaf["filters"], "count": leaf["count"],
                                  "unique_count": len(leaf_rows), "sort_fallback": fallback})
        if len(rows) != total:
            raise ValueError(f"Applied Materials native partitions cover {len(rows)} of {total} rows")
        self.result.pages += requests
        self.result.metadata.setdefault("native_membership_passes", []).append({
            "phase": phase, "total": total, "leaves": leaf_metadata,
            "ids": sorted(rows), "duplicate_ids_across_leaves": duplicate_ids,
            "requests": requests,
        })
        self.result.timings[phase + "_listing_seconds"] = time.perf_counter() - started
        if phase == "initial":
            self.partial_native_rows = list(rows.values())
        return [rows[identity] for identity in sorted(rows, key=int)]

    def preserve_partial_listing(self):
        """Keep exact usable published IDs if initial pagination fails midway."""
        if not self.result.jobs and getattr(self, "partial_native_rows", None):
            for row in self.partial_native_rows:
                self.add(self.native_job(row))
            self.result.metadata["retained_partial_native_listing_rows"] = len(self.partial_native_rows)
            self.result.listing_complete = False

    def native_job(self, row):
        native_row(row)
        return self.job({"listing": row, "journey": "native_global"}, title=row["name"], job_id=str(row["id"]),
                        requisition_id=row["displayJobId"], url=NATIVE + row["positionUrl"],
                        apply_url=NATIVE + "/careers/apply?pid=" + str(row["id"]),
                        locations=row.get("locations", []), department=row.get("department"),
                        date_posted=epoch(row.get("postedTs")), date_posted_raw=row.get("postedTs"),
                        workplace_type=row.get("workLocationOption"), employer="Applied Materials", language="en")

    async def attachments(self, job):
        links = list(dict.fromkeys(urljoin(job.url, n["href"]) for n in BeautifulSoup(job.description_html, "lxml").select("a[href]")
                                   if re.search(r"\.(?:pdf|docx?)(?:[?#]|$)", n["href"], re.I)))
        if links:
            job.raw["attachments"] = []
        for link in links:
            response = await self.http.get(link)
            document = extract_attachment(response.content, urlsplit(link).path)
            job.raw["attachments"].append({"url": link, **document})
            job.description_html += "\n<pre>" + escape(document["text"]) + "</pre>"
        return job

    async def native_detail(self, job):
        if job.job_id not in self.native_ids:
            raise ValueError("Applied Materials exact detail was not exposed in its native listing")
        params = {"position_id": job.job_id, "domain": DOMAIN, "hl": "en"}
        api = NATIVE + "/api/pcsx/position_details"
        source_url = api + "?" + urlencode(params)
        cache = getattr(self, "detail_cache", None)
        cached = cache.get(job.job_id) if cache else None
        if cached and job.job_id not in getattr(self, "force_live_ids", set()):
            response = cached["response"]
            job.raw["detail_provenance"] = {
                "url": source_url, "sha256": cached["sha256"],
                "fetched_at": cached.get("fetched_at"), "reused": True,
                "archive": "responses/" + cached["sha256"] + ".gz",
            }
        else:
            try:
                http_response = await self.http.get(api, params=params, headers=API_HEADERS)
                digest = sha256(http_response.content).hexdigest()
                response = http_response.json()
                job.raw["detail_provenance"] = {
                    "url": str(http_response.request.url), "sha256": digest,
                    "fetched_at": now(), "reused": False,
                    "archive": "responses/" + digest + ".gz",
                }
            except Exception:
                if not cached:
                    raise
                # A blocked live recheck keeps the exact archived response and
                # labels it rather than dropping the record or retrying around
                # the publisher's protection.
                response = cached["response"]
                job.raw["detail_provenance"] = {
                    "url": source_url, "sha256": cached["sha256"],
                    "fetched_at": cached.get("fetched_at"), "reused": True,
                    "archive": "responses/" + cached["sha256"] + ".gz",
                    "live_recheck_failed": True,
                }
        row = response.get("data", {})
        native_row(row)
        listing = job.raw["listing"]
        detail_posted = epoch(row.get("postedTs"))
        listing_posted = epoch(listing.get("postedTs"))
        title_conflict = row.get("name") != listing.get("name")
        if (response.get("status") != 200 or str(row["id"]) != job.job_id
                or any(row.get(key) != listing.get(key) for key in ("displayJobId", "atsJobId", "positionUrl"))
                or (detail_posted is not None and listing_posted is not None
                    and row.get("postedTs") != listing.get("postedTs"))
                or row.get("publicUrl") not in (None, job.url)):
            raise ValueError("Applied Materials exact native detail/listing identity differs")
        if title_conflict:
            # Both published titles are retained; the listing title remains the
            # record title and the exact detail title stays available in raw.
            job.raw["title_conflict"] = {"listing_title": listing.get("name"), "detail_title": row.get("name")}
            job.warnings.append("listing_detail_title_conflict")
        job.raw["detail"] = {k: v for k, v in row.items() if k != "positionUserActions"}
        job.raw["detail_title"] = row.get("name")
        job.description_method = "applied_materials.native_exact.jobDescription"
        job.description_source_url = source_url
        job.raw["date_posted_source"] = "detail.postedTs" if detail_posted is not None else (
            "listing.postedTs" if listing_posted is not None else None
        )
        if row.get("publicUrl") is None:
            job.raw["public_url_source"] = "listing.positionUrl"
        job.date_posted = detail_posted if detail_posted is not None else listing_posted
        job.date_posted_raw = (row.get("postedTs") if detail_posted is not None else
                               listing.get("postedTs") if listing_posted is not None else None)
        job.locations = row.get("locations", [])
        job.department = row.get("department")
        job.workplace_type = row.get("workLocationOption")
        try:
            body = native_body(row)
        except ValueError as exc:
            # The exact public body exists but contains no role content. Keep
            # the raw body for audit, export no invented description, and mark
            # the record explicitly unavailable.
            job.description_html = ""
            job.description_unavailable = True
            job.raw["detail_body_error"] = str(exc)
            job.raw["template_body_html"] = row.get("jobDescription")
            job.description_method = "applied_materials.native_exact.unavailable"
            labels = labeled_fields(row.get("jobDescription") or "")
            job.raw["published_labels"] = labels
            job.employment_type = {k: labels[k] for k in ("Time Type", "Employee Type") if k in labels} or None
            job.salary = native_salary(labels)
            job.raw["talent_pipeline_posting"] = bool(re.search(
                r"use this posting to build our talent pipeline", text_html(row.get("jobDescription") or ""), re.I))
            return job
        job.description_html = body
        labels = labeled_fields(job.description_html)
        job.raw["published_labels"] = labels
        job.employment_type = {k: labels[k] for k in ("Time Type", "Employee Type") if k in labels} or None
        job.salary = native_salary(labels)
        job.raw["talent_pipeline_posting"] = bool(re.search(r"use this posting to build our talent pipeline", text_html(job.description_html), re.I))
        return await self.attachments(job)

    async def sg_snapshot(self, phase):
        response = await self.http.get(CONTRACT + "/sg/")
        config = fwp_config(response.text)
        pager = config["preload_data"]["settings"]["pager"]
        if pager.get("page") != 1 or pager.get("total_pages") != 1:
            raise ValueError("Applied Materials contractor SG pagination changed; follow observed FacetWP next before certification")
        links = sg_links(response.text)
        if len(links) != pager.get("total_rows") or pager.get("total_rows") != pager.get("total_rows_unfiltered"):
            raise ValueError("Applied Materials contractor SG listing count or filtering mismatch")
        self.result.metadata.setdefault("contractor_sg_snapshots", []).append({"phase": phase, "pager": pager, "links": links})
        self.result.pages += 1
        return links

    async def archive_snapshot(self, phase):
        links, pages, seen_pages, titles = {}, [], set(), {}
        url = CONTRACT + "/openings/"
        while url:
            if not self.page_guard() or url in seen_pages:
                raise ValueError("Applied Materials contractor archive pagination repeats or exhausted")
            seen_pages.add(url)
            response = await self.http.get(url)
            if str(response.url) != url:
                raise ValueError("Applied Materials contractor native archive redirected")
            current = sg_links(response.text)
            if not current or set(current) & set(links):
                raise ValueError("Applied Materials contractor archive empty or repeats IDs")
            links.update(current)
            soup = BeautifulSoup(response.text, "lxml")
            for node in soup.select("a[href]"):
                text = node.get_text(" ", strip=True)
                for identity, posting_url in current.items():
                    if node["href"] == posting_url and text and text not in {"See Job & Apply", "Apply Now"}:
                        titles.setdefault(identity, text)
            if any(identity not in titles for identity in current):
                raise ValueError("Applied Materials contractor archive published title missing")
            next_links = [urljoin(url, node["href"]) for node in soup.select("a[href]")
                          if node.get_text(" ", strip=True).startswith("Next")]
            if len(next_links) > 1 or any(not re.fullmatch(r"https://appliedcontract\.jobs/openings/page/\d+/", n) for n in next_links):
                raise ValueError("Applied Materials contractor published next-page route changed")
            pages.append({"url": url, "ids": list(current), "next": next_links})
            url = next_links[0] if next_links else None
        self.result.metadata.setdefault("contractor_archive_snapshots", []).append({"phase": phase, "total": len(links), "pages": pages, "links": links, "titles": titles})
        if phase == "initial":
            self.archive_titles = titles
        return links

    async def sg_detail(self, job):
        response = await self.http.get(job.url)
        if str(response.url) != job.url:
            raise ValueError("Applied Materials contractor native exact posting redirected")
        schema, body, title = sg_fields(response.text, job.requisition_id, job.url)
        if title != job.title:
            raise ValueError("Applied Materials contractor native exact title differs from its published listing")
        job.raw.update(schema=schema, job_body_html=body)
        job.title, job.description_html = title, body
        job.apply_url = job.url.rstrip("/") + "/apply"
        if job.apply_url not in [urljoin(job.url, a["href"]) for a in BeautifulSoup(response.text, "lxml").select("a[href]")]:
            raise ValueError("Applied Materials contractor native exact apply destination changed")
        job.date_posted, job.date_posted_raw = schema.get("datePosted"), schema.get("datePosted")
        # The plugin's exact-one-year schema validThrough is preserved raw. It
        # is not independently established as an actual job closing deadline.
        job.valid_through = None
        job.description_method, job.description_source_url = "applied_materials.raise_native.complete_body", job.url
        job.locations = schema.get("jobLocation") if isinstance(schema.get("jobLocation"), list) else [schema.get("jobLocation")]
        job.employment_type = schema.get("employmentType")
        job.department = schema.get("occupationalCategory")
        job.salary = schema.get("baseSalary")
        plain = text_html(body)
        job.employer = ("Raise Group Public Benefit Corporation" if "Raise Group Public Benefit Corporation" in plain
                        else "Raise Business Solutions Singapore Pte. Ltd." if "Raise Business Solutions" in plain
                        else None)
        return await self.attachments(job)

    async def us_snapshot(self, phase):
        rows, seen, pages, page = [], set(), [], 1
        while True:
            if not self.page_guard():
                raise ValueError("Applied Materials contractor US pagination budget exhausted")
            batch = await self.http.json(US_API + "/job/filteredResults/applied-materials", method="POST", params={"page": page}, json={})
            if not isinstance(batch, list):
                raise ValueError("Applied Materials contractor US native pagination schema changed")
            ids = []
            for row in batch:
                if (not isinstance(row, dict) or not isinstance(row.get("id"), int) or not row.get("title")
                        or row.get("status", {}).get("statusName") != "Open"):
                    raise ValueError("Applied Materials contractor US posting identity or status changed")
                identity = str(row["id"])
                if identity in seen:
                    raise ValueError("Applied Materials contractor US repeated pagination IDs")
                seen.add(identity)
                rows.append(row)
                ids.append(identity)
            pages.append({"page": page, "ids": ids})
            if not batch:
                break
            page += 1
        self.result.metadata.setdefault("contractor_us_snapshots", []).append({"phase": phase, "total": len(rows), "pages": pages})
        return rows

    async def us_detail(self, job):
        if job.requisition_id not in self.us_ids:
            raise ValueError("Applied Materials contractor US detail absent from exact published portal")
        api = US_API + "/job/applied-materials/" + job.requisition_id + "/-1"
        row = await self.http.json(api)
        listing = job.raw["listing"]
        if (str(row.get("id")) != job.requisition_id or row.get("title") != listing.get("title")
                or row.get("isPublished") is not True or row.get("status", {}).get("statusName") != "Open"
                or not row.get("bodyMarkup")):
            raise ValueError("Applied Materials contractor US exact published identity/body mismatch")
        job.raw["detail"] = {k: v for k, v in row.items() if k != "score"}
        job.description_html = (row.get("headerMarkup") or "") + row["bodyMarkup"]
        job.description_method, job.description_source_url = "applied_materials.raise_us.exact_full_markup", api
        job.date_posted, job.date_posted_raw = row.get("postedDate"), row.get("postedDate")
        job.locations = row.get("publish_locations_list", [])
        job.workplace_type = row.get("location_type")
        job.employment_type = "Contract"
        job.employer = "Raise Group Public Benefit Corporation"
        job.salary = {"published_card_text": row.get("company"), "published_location_rates": row.get("publish_locations_list", [])}
        job.apply_url = job.url
        return await self.attachments(job)

    async def run(self):
        try:
            entry = await self.http.get(ENTRY)
            links = [urljoin(str(entry.url), a["href"]) for a in BeautifulSoup(entry.text, "lxml").select("a[href]")]
            if CTA not in links or not any(link.rstrip("/") == CONTRACT for link in links):
                raise ValueError("Applied Materials official published native/contractor journey changed")
        except Exception as exc:
            # Exact current CTA and contractor link independently observed from
            # the official page; the native publisher is validated below.
            if "HTTP 403:" not in str(exc):
                raise
            self.result.metadata["official_entry_http_error"] = str(exc)
        landing = await self.http.get(CTA)
        config = native_config(landing.text)
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION, official_entry=ENTRY,
                                    company_listing_url=NATIVE + "/careers", published_prefiltered_cta=CTA,
                                    official_entry_403_recovery="exact company-published native and contractor links; independent browser evidence required",
                                    native_public_config=config, listing_authority="company native unfiltered Jobs view and two officially linked Raise contractor regions")
        us_page = await self.http.get(CONTRACT + "/")
        if not BeautifulSoup(us_page.text, "lxml").select_one('iframe[src="https://apply-applied-materials.opptly.ai/"]'):
            raise ValueError("Applied Materials official US contractor published iframe changed")
        if CONTRACT + "/sg/" not in [a["href"] for a in BeautifulSoup(us_page.text, "lxml").select("a[href]")]:
            raise ValueError("Applied Materials contractor region membership changed")
        native = await self.snapshot("initial")
        self.native_ids = {str(row["id"]) for row in native}
        sg = await self.sg_snapshot("initial")
        archive = await self.archive_snapshot("initial")
        if any(archive.get(identity) != url for identity, url in sg.items()):
            raise ValueError("Applied Materials contractor SG membership differs from published all-jobs archive")
        us = await self.us_snapshot("initial")
        self.us_ids = {str(row["id"]) for row in us}
        self.result.expected_total = len(native) + len(archive) + len(us)
        for row in native:
            self.add(self.native_job(row))
        for identity, url in archive.items():
            self.add(self.job({"journey": "contractor_archive", "listing_url": CONTRACT + "/openings/", "in_singapore_view": identity in sg, "listing_title": self.archive_titles[identity]}, title=self.archive_titles[identity], url=url,
                              job_id="contract-native-" + identity, requisition_id=identity, language="en"))
        for row in us:
            self.add(self.job({"journey": "contractor_us", "listing": {k: v for k, v in row.items() if k != "appliedAt"}},
                              title=row["title"], url=US + "/job?id=" + str(row["id"]),
                              job_id="contract-us-" + str(row["id"]), requisition_id=str(row["id"]), language="en"))
        if self.options.max_jobs:
            self.result.jobs = self.result.jobs[:self.options.max_jobs]
            self.result.warnings.append("sample_limit")
        async def detail(job):
            return await {"native_global": self.native_detail, "contractor_archive": self.sg_detail, "contractor_us": self.us_detail}[job.raw["journey"]](job)
        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        final_native = await self.snapshot("final")
        final_sg = await self.sg_snapshot("final")
        final_archive = await self.archive_snapshot("final")
        final_us = await self.us_snapshot("final")
        if ({str(r["id"]) for r in final_native} != self.native_ids or final_sg != sg or final_archive != archive
                or {str(r["id"]) for r in final_us} != self.us_ids):
            raise ValueError("Applied Materials initial/final global/contractor membership changed")
        self.result.metadata["journey_totals"] = {"native_global": len(native), "contractor_native_archive": len(archive), "contractor_sg_subset": len(sg), "contractor_us": len(us)}
        self.result.listing_complete = "sample_limit" not in self.result.warnings
        return self.result
