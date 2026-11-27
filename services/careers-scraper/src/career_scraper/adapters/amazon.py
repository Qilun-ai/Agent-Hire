"""Amazon's company-owned search, including complete facet partitions above its search cap.

Also parses the hourly-directory native HVH journeys: country landings on
amazon.jobs that publish exact Salesforce ``BBJobDetails?reqid=...`` links
(legacy ``.sfdc_richtext`` and modular ``section.custom-hw-section`` bodies).
SPA /app#jobSearch landings (hiring.amazon.* etc.) require a WAF-token
GraphQL client and are not enumerated here; see journey-research.md.
"""
import re
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import text_html
from .base import Adapter
from .html import from_schema, job_postings

HVH_DETAIL_HOST = "hvr-amazon.my.site.com"
HVH_GENERIC_HEADINGS = {"Explorer Amazon Jobs", "Poznaj Amazon Jobs", "Amazon High Volume Hiring", "WFS"}
_HVH_AGENCIES = ("Adecco", "Randstad", "Gi Group", "Manpower", "Partnaire", "Job and Talent", "Kariyer.net")


def parse_hvh_landing_links(html):
    """Unique exact HVH detail links published on a native amazon.jobs landing."""
    soup = BeautifulSoup(html, "lxml")
    seen, links = set(), []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "BBJobDetails" not in href:
            continue
        reqids = parse_qs(urlparse(href).query).get("reqid") or []
        if not reqids or reqids[0] in seen:
            continue
        seen.add(reqids[0])
        links.append({"reqid": reqids[0], "url": href, "link_text": anchor.get_text(" ", strip=True)})
    return links


def _hvh_public_number(text):
    for pattern in (r"(?:Identification du poste|Identifika\S+ \S+ pracovn\S+ m\S+sta|Identyfikator stanowiska|Job ID|Identifika\S+)\s*:?\s*(\d{9})",
                    r"\b(\d{9})\b"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _hvh_agency(link_text, body_text):
    for match in re.finditer(r"\(([^()]{1,40})\)", link_text or ""):
        candidate = match.group(1).strip()
        if candidate == "Amazon" or candidate in _HVH_AGENCIES:
            return candidate
    for agency in _HVH_AGENCIES:
        if agency in body_text or agency in (link_text or ""):
            return agency
    return None


def parse_hvh_detail(html, *, reqid, url, link_text="", landing="", landing_url=""):
    """Parse one exact HVH detail page. Returns a dict; ``is_gateway`` ids have no body."""
    base = {"job_id": reqid, "reqid": reqid, "url": url, "apply_url": url,
            "link_text": link_text, "landing": landing, "landing_url": landing_url,
            "company": "Amazon", "source": "amazon.hvh", "date_posted": None, "date_posted_raw": None}
    soup = BeautifulSoup(html, "lxml")
    legacy = soup.select_one(".sfdc_richtext")
    sections = [s for s in soup.select("section.custom-hw-section")
                if "hw-hidden" not in (s.get("class") or [])]
    if not legacy and not sections:
        base.update({"is_gateway": True, "title": link_text or None, "description_html": "",
                     "description_text": "", "description_method": None, "description_verified": False,
                     "warnings": ["hvh_gateway_no_body"]})
        return base
    body_html = str(legacy) if legacy else "\n".join(str(s) for s in sections)
    body_text = text_html(body_html)
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if title in HVH_GENERIC_HEADINGS or not title:
        h2 = soup.find("h2")
        title = h2.get_text(" ", strip=True) if h2 else (link_text or "")
    location = None
    loc_section = next((s for s in sections if "location-section" in (s.get("class") or [])), None)
    if loc_section is not None:
        location = loc_section.get_text(" ", strip=True)
    salary = None
    sal_section = next((s for s in sections if "salary-section" in (s.get("class") or [])), None)
    if sal_section is not None:
        salary = sal_section.get_text(" ", strip=True)
    public_number = _hvh_public_number(soup.get_text(" ", strip=True))
    # Legacy pages print location in prose without a structured field; leave
    # location null rather than guessing a substring of the body text.
    method = "amazon.hvh.detail_sfdc_text" if legacy else "amazon.hvh.detail_sections"
    verified = len(body_text.strip()) >= 100 and bool(title)
    base.update({"is_gateway": False, "title": title, "public_job_number": public_number,
                 "requisition_id": public_number, "description_html": body_html,
                 "description_text": body_text, "description_method": method,
                 "description_source_url": url, "description_verified": verified,
                 "locations": [location] if location else [], "salary": salary,
                 "employer": _hvh_agency(link_text, body_text),
                 "setlang": (parse_qs(urlparse(url).query).get("setlang") or [None])[0],
                 "warnings": [] if verified else ["short_or_missing_description"]})
    return base


def parse_amazon_detail_sections(html):
    """Corporate detail-page sections keyed by heading (Description/Basic/Preferred)."""
    soup = BeautifulSoup(html, "lxml")
    sections = {}
    for heading in soup.find_all("h2"):
        name = heading.get_text(strip=True)
        if name in {"Description", "Basic Qualifications", "Preferred Qualifications"}:
            sections[name] = str(heading.parent)
    return sections


class Amazon(Adapter):
    API = "https://www.amazon.jobs/en/search.json"
    SEARCH_CAP = 10000
    PAGE_SIZE = 100
    # These fields are single-valued on a posting. State/city facets overlap and cannot certify totals.
    PARTITIONS = {"normalized_country_code": "country_code", "business_category": "business_category"}

    def params(self, filters, offset=0, limit=None):
        params = [("result_limit", limit or self.PAGE_SIZE), ("offset", offset), ("sort", "recent"),
                  ("base_query", ""), ("loc_query", "")]
        params += [(f"{name}[]", value) for name, value in sorted(filters.items())]
        params += [("facets[]", name) for name in self.PARTITIONS]
        return params

    @staticmethod
    def buckets(data, name):
        rows = data.get("facets", {}).get(name + "_facet", [])
        result = {}
        for row in rows:
            if not isinstance(row, dict) or len(row) != 1:
                return {}
            value, count = next(iter(row.items()))
            if not value or value in result or not isinstance(count, int) or count < 0:
                return {}
            if count:
                result[value] = count
        return result

    async def fetch(self, filters, offset=0, limit=None):
        if not self.page_guard():
            raise ValueError("Amazon page budget exhausted")
        params = self.params(filters, offset, limit)
        data = await self.http.json(self.API, params=params)
        if data.get("error"):
            raise ValueError(f"Amazon search error: {data['error']}")
        if not isinstance(data.get("jobs"), list) or not isinstance(data.get("hits"), int):
            raise ValueError("Amazon search response schema changed")
        return data, self.API + "?" + urlencode(params)

    def add_rows(self, rows, source_url, filters):
        ids = set()
        for row in rows:
            for name, value in filters.items():
                if row.get(self.PARTITIONS[name]) != value:
                    message = f"amazon_partition_filter_not_honored: {name}={value}"
                    if message not in self.result.errors:
                        self.result.errors.append(message)
            desc = row.get("description") or ""
            for field in ("basic_qualifications", "preferred_qualifications"):
                if row.get(field):
                    desc += f"<h2>{escape(field.replace('_', ' ').title())}</h2>{row[field]}"
            complete_fields = bool(row.get("description")) and all(
                field in row for field in ("description", "basic_qualifications", "preferred_qualifications"))
            posted = None
            if row.get("posted_date"):
                try:
                    posted = datetime.strptime(" ".join(row["posted_date"].split()), "%B %d, %Y").date().isoformat()
                except ValueError:
                    pass
            job = self.job(row, title=row["title"], job_id=row.get("id_icims") or row.get("id"),
                url=urljoin("https://www.amazon.jobs", row["job_path"]), description=desc,
                method="amazon.search.full_sections" if complete_fields else None, description_url=source_url,
                locations=[row.get("location")], date_posted=posted, date_posted_raw=row.get("posted_date"),
                department=row.get("job_category"), employer=row.get("company_name"),
                qualifications={k: row.get(k) for k in ("basic_qualifications", "preferred_qualifications")})
            ids.add(job.job_id or job.url)
            self.add(job)
            if self.limit():
                break
        return ids

    def partition(self, data, filters):
        candidates = []
        for name in self.PARTITIONS:
            if name in filters:
                continue
            values = self.buckets(data, name)
            if len(values) > 1 and sum(values.values()) >= data["hits"]:
                candidates.append((name, values))
        if not candidates:
            return None
        if len({sum(values.values()) for _, values in candidates}) > 1:
            self.result.errors.append(f"amazon_facet_totals_disagree: {filters}")
        return min(candidates, key=lambda pair: (max(pair[1].values()), len(pair[1])))

    async def run(self):
        partitions = self.result.metadata.setdefault("partitions", [])
        root_data = None

        async def crawl(filters, hint=None):
            nonlocal root_data
            data, source_url = await self.fetch(filters)
            if not filters:
                root_data = data
                self.result.metadata["advertised_total"] = data["hits"]
            local_ids = self.add_rows(data["jobs"], source_url, filters)
            total = data["hits"]
            if "sample_limit" in self.result.warnings:
                return total, False
            if total >= self.SEARCH_CAP and not (hint is not None and hint <= self.SEARCH_CAP):
                selected = self.partition(data, filters)
                if not selected:
                    self.result.errors.append(f"amazon_search_cap_unresolved: {filters}")
                    return total, False
                name, values = selected
                derived_total = sum(values.values())
                self.result.warnings.append("amazon_facet_partition_used")
                partitions.append({"filters": filters, "facet": name, "expected_total": derived_total,
                                   "values": values})
                complete = True
                for value, count in values.items():
                    child_total, child_complete = await crawl({**filters, name: value}, count)
                    if child_total != count:
                        self.result.errors.append(f"amazon_partition_total_changed: {name}={value}: {count} -> {child_total}")
                    complete = child_complete and complete
                    if "sample_limit" in self.result.warnings:
                        return derived_total, False
                return derived_total, complete
            if hint is not None and total != hint:
                self.result.errors.append(f"amazon_partition_count_mismatch: {filters}: {hint} -> {total}")
            offset = len(data["jobs"])
            while len(local_ids) < total:
                if not data["jobs"]:
                    self.result.errors.append(f"amazon_pagination_incomplete: {filters}: {len(local_ids)}/{total}")
                    return total, False
                data, source_url = await self.fetch(filters, offset)
                if data["hits"] != total:
                    self.result.errors.append(f"amazon_total_changed_during_crawl: {filters}: {total} -> {data['hits']}")
                before = len(local_ids)
                local_ids.update(self.add_rows(data["jobs"], source_url, filters))
                if "sample_limit" in self.result.warnings:
                    return total, False
                if len(local_ids) == before:
                    self.result.errors.append(f"amazon_repeated_or_empty_page: {filters}: {offset}")
                    return total, False
                offset += len(data["jobs"])
            if len(local_ids) != total:
                self.result.errors.append(f"amazon_leaf_total_mismatch: {filters}")
                return total, False
            return total, True

        try:
            expected, complete = await crawl({})
            self.result.expected_total = expected
            if partitions and complete:
                self.result.metadata["total_basis"] = "single_valued_facet_sum"
                final, _ = await self.fetch({}, limit=1)
                name = partitions[0]["facet"]
                if self.buckets(final, name) != self.buckets(root_data, name):
                    self.result.errors.append("amazon_facet_counts_changed_during_crawl")
            self.result.listing_complete = complete and len(self.result.jobs) == expected
        except Exception as exc:
            self.result.errors.append(f"amazon_listing_failed: {type(exc).__name__}: {exc}")
        # The search API exposes full sections, not description_short. Use the detail page when fields are missing.
        pending = [j for j in self.result.jobs if not j.description_method]
        if pending:
            originals = [j.url for j in pending]
            hydrated = await self.hydrate(pending, self.detail)
            replacements = dict(zip(originals, hydrated))
            self.result.jobs[:] = [replacements.get(j.url, j) for j in self.result.jobs]
        self.result.warnings = list(dict.fromkeys(self.result.warnings))
        return self.result

    async def detail(self, job):
        response = await self.http.get(job.url)
        rows = job_postings(response.text)
        if rows:
            full = from_schema(self.company.name, "amazon", rows[0], str(response.url))
            full.raw["listing"] = job.raw
            full.job_id = full.job_id or job.job_id
            return full
        soup = BeautifulSoup(response.text, "lxml")
        headings = [h for h in soup.find_all("h2") if h.get_text(strip=True) in
                    {"Description", "Basic Qualifications", "Preferred Qualifications"}]
        if len(headings) != 3:
            raise ValueError("Amazon full description sections missing")
        job.description_html = "\n".join(str(h.parent) for h in headings)
        job.description_text = ""
        job.description_method, job.description_source_url = "amazon.detail_sections", str(response.url)
        return job


# --- Regional partner journeys linked from amazon.jobs/content/en/hourly ---

def parse_entregapro_jobs(payload):
    """Parse the official EntregaPro (Mexico DSP) JSON search response.

    The page is linked from Amazon's hourly directory. Records are partner
    postings: ``company`` is the legal DSP employer, never relabelled Amazon.
    """
    jobs = []
    for record in payload.get("records") or []:
        desc = record.get("description") or ""
        for field, heading in (("qualifications", "Qualifications"),
                               ("responsibilities", "Responsibilities")):
            value = record.get(field)
            if value:
                desc += f"<h2>{heading}</h2>{value}"
        locations = []
        for loc in record.get("locations") or []:
            text = ", ".join(str(loc.get(k)) for k in ("city", "state") if loc.get(k))
            if text and text not in locations:
                locations.append(text)
        if not locations:
            text = ", ".join(str(record.get(k)) for k in ("city", "state") if record.get(k))
            if text:
                locations.append(text)
        posted = (record.get("startDate") or "")[:10] or None
        jobs.append({
            "job_id": record.get("id"), "requisition_id": record.get("referenceNumber") or record.get("externalId"),
            "title": record.get("title"), "url": record.get("url"),
            "apply_url": record.get("externalApplyUrl") or record.get("careerSiteApplyUrl"),
            "external_url": record.get("externalUrl"),
            "description_html": desc, "description_text": text_html(desc),
            "description_method": "amazon.entregapro.jobs_api_description",
            "description_source_url": record.get("url"), "description_verified": bool(text_html(desc).strip()),
            "locations": locations, "employer": record.get("company"),
            "employment_type": record.get("employmentType"), "department": record.get("category"),
            "salary": record.get("salary"), "date_posted": posted,
            "date_posted_raw": record.get("startDate"), "valid_through": record.get("endDate"),
            "qualifications": record.get("qualifications"), "responsibilities": record.get("responsibilities"),
            "experience": record.get("experience"), "status": record.get("status"),
            "language": "es", "source": "amazon.entregapro",
            "warnings": [] if len(text_html(desc).strip()) >= 100 else ["short_or_missing_description"],
        })
    return jobs


def parse_dspjobhub_list(html):
    """Parse one dspjobhub.com /jobs listing page (Attrax job board)."""
    soup = BeautifulSoup(html, "lxml")
    cards = []
    for tile in soup.select("div.attrax-vacancy-tile"):
        anchor = tile.select_one("a.attrax-vacancy-tile__title")
        if not anchor or not anchor.get("href"):
            continue

        def value(selector):
            node = tile.select_one(selector)
            return node.get_text(" ", strip=True) if node else None

        cards.append({
            "job_id": tile.get("data-jobid"), "url": urljoin("https://dspjobhub.com", anchor["href"]),
            "title": anchor.get_text(" ", strip=True), "salary": value(".attrax-vacancy-tile__salary-value"),
            "location": value(".attrax-vacancy-tile__location-freetext .attrax-vacancy-tile__item-value"),
            "dsp_name": value(".attrax-vacancy-tile__option-brand .attrax-vacancy-tile__item-value"),
            "hire_type": value(".attrax-vacancy-tile__option-hire-type .attrax-vacancy-tile__item-value"),
            "job_type": value(".attrax-vacancy-tile__option-job-type .attrax-vacancy-tile__item-value"),
            "location_state": value(".attrax-vacancy-tile__option-location .attrax-vacancy-tile__item-value"),
            "description_preview": value(".attrax-vacancy-tile__description"),
            "reference": value(".attrax-vacancy-tile__reference .attrax-vacancy-tile__item-value"),
            "expiry": value(".attrax-vacancy-tile__expiry .attrax-vacancy-tile__item-value"),
        })
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    match = re.search(r"([\d,]+) Job\(s\) found", text)
    total = int(match.group(1).replace(",", "")) if match else None
    return cards, total


def parse_dspjobhub_detail(html, card):
    """Parse one dspjobhub.com job detail page. Employer is the named DSP."""
    soup = BeautifulSoup(html, "lxml")
    desc = soup.select_one('div.description-widget div[aria-label="Job description"]')
    desc_html = str(desc) if desc else ""
    options = {}
    for widget in soup.select("div.vacancy-options-widget div.attrax-vacancy-tile__item"):
        label = widget.select_one(".attrax-vacancy-tile__item-label")
        value = widget.select_one(".attrax-vacancy-tile__item-value")
        if label and value:
            options[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else card.get("title")
    apply_url = None
    for anchor in soup.find_all("a", href=True):
        if anchor.get_text(" ", strip=True).lower() in ("apply now", "apply"):
            apply_url = urljoin(card.get("url", "https://dspjobhub.com"), anchor["href"])
            break
    brand = next((v for k, v in options.items() if "brand" in k.lower()), None)
    expiry = card.get("expiry")
    valid_through = None if not expiry or expiry.startswith("01/01/0001") else expiry
    return {
        "title": title, "description_html": desc_html, "description_text": text_html(desc_html),
        "description_method": "amazon.dspjobhub.detail_description_widget",
        "description_source_url": card.get("url"), "description_verified": len(text_html(desc_html).strip()) >= 100,
        "apply_url": apply_url, "employer": brand or card.get("dsp_name"),
        "job_type": options.get("Job Type") or card.get("job_type"),
        "hire_type": options.get("Hire Type") or card.get("hire_type"),
        "salary": options.get("SalaryText") or options.get("Salary") or card.get("salary"),
        "locations": [options.get("LocationText") or card.get("location")] if (options.get("LocationText") or card.get("location")) else [],
        "options": options, "valid_through": valid_through, "date_posted": None,
        "warnings": [] if len(text_html(desc_html).strip()) >= 100 else ["short_or_missing_description"],
    }


def parse_spa_job(detail, schedules=None, card=None):
    """Normalise one Amazon hourly SPA job (getJobDetail + schedule cards)."""
    detail = detail or {}
    card = card or {}
    schedules = schedules or []
    desc_html = detail.get("jobDescription") or ""
    locations = []
    for schedule in schedules:
        loc = ", ".join(str(schedule.get(k)) for k in ("city", "state") if schedule.get(k))
        if loc and loc not in locations:
            locations.append(loc)
    if not locations and detail.get("locationName"):
        locations.append(detail["locationName"])
    pay_values = [s.get("totalPayRate") or s.get("basePay") for s in schedules]
    pay_values = [v for v in pay_values if isinstance(v, (int, float))]
    salary = None
    if pay_values:
        salary = {"currency": schedules[0].get("currencyCode"), "unit": "HOUR",
                  "min": min(pay_values), "max": max(pay_values)}
    elif detail.get("compliancePayRangeMin") or detail.get("compliancePayRangeMax"):
        salary = {"currency": detail.get("currencyCode"), "unit": detail.get("compliancePayRangeFrequency"),
                  "min": detail.get("compliancePayRangeMin"), "max": detail.get("compliancePayRangeMax")}
    posted = detail.get("mostRecentPostedDate")
    return {
        "job_id": detail.get("jobId") or card.get("jobId"),
        "requisition_id": detail.get("jobIdNumber") or card.get("jobId"),
        "title": detail.get("jobTitle") or card.get("jobTitle"),
        "url": f"https://hiring.amazon.com/app#/jobDetail?jobId={detail.get('jobId') or card.get('jobId')}",
        "apply_url": f"https://hiring.amazon.com/app#/jobDetail?jobId={detail.get('jobId') or card.get('jobId')}",
        "description_html": desc_html, "description_text": text_html(desc_html),
        "description_method": "amazon.spa.getJobDetail_jobDescription",
        "description_source_url": "https://hiring.amazon.com/graphql#getJobDetail",
        "description_verified": len(text_html(desc_html).strip()) >= 100,
        "locations": locations, "employer": detail.get("agencyName") or "Amazon",
        "employment_type": detail.get("employmentTypeL10N") or detail.get("employmentType"),
        "department": (schedules[0].get("scheduleBusinessCategory") if schedules else None),
        "salary": salary, "date_posted": posted, "date_posted_raw": posted,
        "qualifications": detail.get("jobQualification"),
        "valid_through": None, "language": (detail.get("requiredLanguage") or [None])[0],
        "status": detail.get("postingStatus"), "schedules": schedules,
        "warnings": [] if len(text_html(desc_html).strip()) >= 100 else ["short_or_missing_description"],
    }
