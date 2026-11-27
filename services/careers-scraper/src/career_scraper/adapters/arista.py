"""Arista's own PUBLIC SmartRecruiters widget and only its exact native postings."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape, unescape
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import access
from .base import Adapter
from ..attachments import extract_attachment
from ..models import text_html

ENTRY = "https://www.arista.com/en/careers"
WIDGET_SCRIPT = "https://static.smartrecruiters.com/job-widget/1.8.5/script/smart_widget.js"
LISTING = "https://www.smartrecruiters.com/job-api/public/search/widgets/AristaNetworks/postings"
BOARD = "AristaNetworks"
DEPARTMENT_FIELD = "58b81d3de4b06dbf7dd514e1"


def compact(value):
    return re.sub(r"[\s\ufeff]", "", value or "")


class WidgetChallenge(ValueError):
    """The official widget page served a JS challenge instead of the config."""


def widget_config(html):
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.get_text(strip=True) == "Client Challenge":
        raise WidgetChallenge("Arista official careers source returned a JavaScript Client Challenge")
    scripts = soup.select("script.job_widget")
    if len(scripts) != 1 or not any(n.get("src") == WIDGET_SCRIPT for n in soup.select("script[src]")):
        raise ValueError("Arista company-published listing widget is missing or changed")
    match = re.fullmatch(r"\s*widget\((\{.*\})\);?\s*", scripts[0].get_text(), re.S)
    if not match:
        raise ValueError("Arista widget configuration is not the inspected public JSON")
    config = json.loads(match[1])
    if (config.get("company_code") != BOARD or config.get("api_url") != "https://www.smartrecruiters.com"
            or config.get("jobAdType") != "PUBLIC" or config.get("auth") or config.get("group_id")
            or config.get("departments_field_id") != DEPARTMENT_FIELD):
        raise ValueError("Arista exact company widget identity, public scope or department field changed")
    return config


def listing_params(offset, config):
    departments = config.get("filter_departments") or []
    if isinstance(departments, str):
        departments = [departments]
    locations = config.get("filter_locations") or []
    if isinstance(locations, str):
        locations = [locations]
    return {"dcr_ci": BOARD, "offset": offset, "limit": int(config.get("jobs_number") or 100),
            "jobAdType": "PUBLIC", "fq": "location:(" + ";".join(locations) + ")" if locations else "",
            "customFields": ",".join(DEPARTMENT_FIELD + "|" + str(d) for d in departments),
            "langCode": "en", "locationType": "ANY"}


def native_url(row):
    identity, slug = str(row["publicationId"]), row.get("urlJobName")
    return f"https://jobs.smartrecruiters.com/{BOARD}/{identity}" + ("-" + quote(slug, safe="-") if slug else "")


def parse_oneclick(soup):
    values = [s.get_text() for s in soup.select("script") if "window.ONECLICKDATA =" in s.get_text()]
    if len(values) != 1:
        raise ValueError("Arista native posting exact application configuration is missing")
    block = re.search(r"window\.ONECLICKDATA\s*=\s*\{(.*?)\};", values[0], re.S)
    if not block:
        raise ValueError("Arista native application configuration changed")
    out = {}
    for key in ("cident", "vid", "pid", "puuid", "region", "country", "url"):
        value = re.search(r"(?:^|,)\s*" + key + r"\s*:\s*(?:'([^']*)'|(\d+))", block[1], re.S)
        if not value:
            raise ValueError("Arista native application field missing: " + key)
        out[key] = value[1] if value[1] is not None else value[2]
    return out


def native_fields(html, row):
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one('main.job[itemtype="http://schema.org/JobPosting"]')
    if main is None:
        raise ValueError("Arista exact native job body missing; homepage/challenge is not a description")
    apply = parse_oneclick(soup)
    for key, expected in {"cident": BOARD, "pid": str(row["publicationId"]),
                          "vid": str(row["jobVacancyId"]), "puuid": row["uuid"]}.items():
        if apply[key] != expected:
            raise ValueError("Arista native/listing identity mismatch: " + key)
    title = main.select_one('h1[itemprop="title"]')
    employer = main.select_one('[itemprop="hiringOrganization"] [itemprop="name"]')
    if (title is None or compact(title.get_text()) != compact(row["vacancyName"])
            or employer is None or employer.get("content") != "Arista Networks"):
        raise ValueError("Arista native/listing title or employer mismatch")
    containers = main.select('.job-sections > [itemprop="description"]')
    if len(containers) != 1:
        raise ValueError("Arista full company/job/qualification/additional body is missing or ambiguous")
    body = containers[0]
    sections = body.select("section.job-section")
    ids = [n.get("id") for n in sections]
    if (not sections or any(not i for i in ids) or len(ids) != len(set(ids))
            or "st-jobDescription" not in ids or len(text_html(str(body))) < 100):
        raise ValueError("Arista native role sections are missing or incomplete")
    # No author-written section outside the full description may silently vanish.
    extras = [n for n in main.select(".job-sections section.job-section[id]") if body not in n.parents]
    if any(n.get("id") != "st-videos" or n.get_text(strip=True) for n in extras):
        raise ValueError("Arista published description section lies outside captured full body")
    videos = [{"url": n.get("href"), "title": n.get("title"), "label": n.get("aria-label")}
              for extra in extras for n in extra.select("a[href]")]
    metadata = [{"itemprop": n["itemprop"], "value": n.get("content") or n.get_text(" ", strip=True)}
                for n in main.select("meta[itemprop]")]
    for key in ("datePosted", "validThrough"):
        if sum(m["itemprop"] == key for m in metadata) > 1:
            raise ValueError("Arista public timestamp is ambiguous: " + key)
    dates = {m["itemprop"]: m["value"] for m in metadata}
    posted = dates.get("datePosted") or None
    if posted:
        native_stamp = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        if native_stamp.tzinfo is None:
            raise ValueError("Arista posted timestamp has no timezone")
        if abs(native_stamp.timestamp() * 1000 - row["releasedDate"]) > 1:
            raise ValueError("Arista native/listing published timestamp mismatch")
    template = "https://jobs.smartrecruiters.com/oneclick-ui/company/%s/publication/%s?dcr_ci=%s"
    if apply["url"] != template:
        raise ValueError("Arista native exact apply template changed")
    apply_url = template.replace("%s", BOARD, 1).replace("%s", apply["puuid"], 1).replace("%s", BOARD, 1)
    descriptions = [{"id": n["id"], "title": n.select_one("h2").get_text(" ", strip=True) if n.select_one("h2") else None,
                     "html": str(n), "text": text_html(str(n))} for n in sections]
    location = main.select_one("spl-job-location")
    public_location = dict(location.attrs) if location else {}
    address = {m["itemprop"]: m["value"] for m in metadata if m["itemprop"].startswith("address")}
    public_location.update(address)
    employment = main.select_one('[itemprop="employmentType"]')
    salary_nodes = body.select(".salary,.salary-range,.salary-value")
    pay_paragraphs = [n.get_text(" ", strip=True) for n in body.select("p")
                      if re.search(r"compensation information|(?:base|annual|salary|pay)\s+(?:pay|salary|range)|salary range", n.get_text(" ", strip=True), re.I)]
    salary = {"text": list(dict.fromkeys([n.get_text(" ", strip=True) for n in salary_nodes] + pay_paragraphs)),
              "basis": "Company-published compensation paragraphs, without inferred bounds"} if salary_nodes or pay_paragraphs else None
    updated = soup.select_one('meta[itemprop="og:updated_time"]')
    return {"title": title.get_text(" ", strip=True), "description_html": str(body), "sections": descriptions,
            "apply": apply, "apply_url": apply_url, "metadata": metadata,
            "locations": [public_location], "employment_type": employment.get_text(" ", strip=True) if employment else None,
            "date_posted": posted, "date_updated": updated.get("content") if updated else None,
            "valid_through": dates.get("validThrough") or None, "salary": salary,
            "language": soup.html.get("lang") if soup.html else row.get("languageCode"),
            "videos": videos, "attachments": list(dict.fromkeys(urljoin(native_url(row), a["href"]) for a in body.select("a[href]")
                                      if re.search(r"\.(?:pdf|docx?)(?:[?#]|$)", a["href"], re.I)))}


def university_programs(html, url):
    """Preserve exact public intake tabs as programs, without inventing vacancies."""
    soup = BeautifulSoup(html, "lxml")
    tabs = soup.select("#tabList a[data-tab][href]")
    programs = []
    for tab in tabs:
        path = tab["href"]
        if not path.startswith("/en/careers/university/"):
            continue
        section = soup.select_one("#" + tab["data-tab"])
        if section is None:
            raise ValueError("Arista published university tab has no full body")
        body = BeautifulSoup(str(section), "lxml")
        for script in body.select("script"):
            # Decode only the publisher's observed concatenated literal email
            # display value. Never execute arbitrary page JavaScript.
            match = re.search(r"var addy_text([0-9a-f]+)\s*=\s*(.*?);document", script.get_text(), re.S)
            if match:
                email = unescape("".join(re.findall("'([^']*)'", match[2])))
                cloak = body.select_one("#cloak" + match[1])
                if cloak is not None and re.fullmatch(r"[\w.+-]+@arista\.com", email):
                    cloak.string = email
        for node in body.select("script,style"):
            node.decompose()
        content = str(body.select_one(".tab-pane"))
        text = text_html(content)
        programs.append({"title": tab.get_text(" ", strip=True), "url": urljoin(url, path),
                         "content_section_id": tab["data-tab"], "job_id": None, "requisition_id": None,
                         "date_posted": None, "valid_through": None,
                         "kind": "university_program_and_evergreen_intake", "counted_in_requisition_listing": False,
                         "application_emails": list(dict.fromkeys(re.findall(r"[\w.+-]+@arista\.com", text))),
                         "description_html": content, "description_text": text,
                         "description_sha256": sha256(text.encode()).hexdigest()})
    if len(programs) != 3 or len({p["url"] for p in programs}) != 3:
        raise ValueError("Arista university program membership changed; individual scope review required")
    return programs


class Arista(Adapter):
    VERIFICATION_REVISION = 2

    def validate_listing_row(self, row):
        if (not isinstance(row, dict) or row.get("companyIdentifier") != BOARD
                or row.get("companyName") != "Arista Networks" or row.get("destinationCode") != "WIDGET"
                or not re.fullmatch(r"\d+", str(row.get("publicationId", "")))
                or not re.fullmatch(r"\d+", str(row.get("jobVacancyId", "")))
                or not re.fullmatch(r"[0-9a-f-]{36}", row.get("uuid", "")) or not row.get("vacancyName")
                or not isinstance(row.get("releasedDate"), int)):
            raise ValueError("Arista widget contains invalid/wrong-company/publication identity")

    async def snapshot(self, phase, config):
        started, rows, pages, total, offset = time.perf_counter(), [], [], None, 0
        while True:
            if not self.page_guard():
                raise ValueError("Arista company-widget page budget exhausted")
            params = listing_params(offset, config)
            data = await self.http.json(LISTING, params=params)
            count, items = data.get("numFound"), data.get("results")
            if total is None:
                total = count
            if (not isinstance(total, int) or total < 0 or count != total or not isinstance(items, list)
                    or len(items) != min(params["limit"], max(0, total - offset))):
                raise ValueError("Arista widget count changed or pagination is incomplete")
            for row in items:
                self.validate_listing_row(row)
                if row["publicationId"] in {r["publicationId"] for r in rows}:
                    raise ValueError("Arista widget repeats publication across pages")
                rows.append(row)
            pages.append({"offset": offset, "count": len(items), "total": total,
                          "ids": [r["publicationId"] for r in items], "params": params})
            if len(rows) == total:
                break
            offset += params["limit"]
        self.result.metadata.setdefault("membership_snapshots", []).append(
            {"phase": phase, "pages": pages, "ids": [r["publicationId"] for r in rows], "total": total})
        self.result.timings[phase + "_membership_seconds"] = time.perf_counter() - started
        return rows

    async def detail(self, job):
        row = job.raw["listing"]
        if row["publicationId"] not in self.published_ids or job.url != native_url(row):
            raise ValueError("Arista detail was not exposed by the official company widget")
        response = await self.http.get(job.url, headers={"Accept": "text/html"})
        if (urlsplit(str(response.url)).hostname != "jobs.smartrecruiters.com"
                or not urlsplit(str(response.url)).path.startswith(f"/{BOARD}/{job.job_id}-")):
            raise ValueError("Arista listed posting redirects away from its exact company publication")
        fields = native_fields(response.text, row)
        job.raw["native"] = {k: v for k, v in fields.items() if k != "description_html"}
        job.description_html = fields["description_html"]
        job.apply_url = fields["apply_url"]
        job.locations = fields["locations"]
        job.employment_type = fields["employment_type"]
        job.date_posted = fields["date_posted"]
        job.date_posted_raw = {"datePosted": fields["date_posted"], "releasedDate": row["releasedDate"]}
        job.date_updated, job.valid_through = fields["date_updated"], fields["valid_through"]
        job.salary, job.language = fields["salary"], fields["language"] or row.get("languageCode")
        job.qualifications = next((s["html"] for s in fields["sections"] if s["id"] == "st-qualifications"), None)
        job.responsibilities = next((s["html"] for s in fields["sections"] if s["id"] == "st-jobDescription"), None)
        job.description_method, job.description_source_url = "arista.company_widget.exact_native_full_description", str(response.url)
        for url in fields["attachments"]:
            attachment = await self.http.get(url)
            document = extract_attachment(attachment.content, urlsplit(url).path)
            document.update(url=url, sha256=sha256(attachment.content).hexdigest())
            job.raw.setdefault("attachments", []).append(document)
            job.description_html += "\n<pre>" + escape(document["text"]) + "</pre>"
        return job

    async def inspect_journeys(self, soup):
        journeys = list(dict.fromkeys(urljoin(ENTRY, a["href"]) for a in soup.select("a[href]")
                                     if a["href"].startswith("/en/careers/") and "industry-recognition" not in a["href"]))
        evidence = []
        for url in journeys:
            response = await self.http.get(url)
            page = BeautifulSoup(response.text, "lxml")
            if page.select("script.job_widget"):
                config = widget_config(response.text)
                evidence.append({"url": url, "final_url": str(response.url), "widget": config,
                                 "same_company_global_listing_filtered_view": True})
            elif url.endswith("/university"):
                # Program/intake sections are retained for explicit review, never
                # turned into invented SmartRecruiters requisitions.
                content = page.select_one("#dataTab")
                if content is None:
                    raise ValueError("Arista university program tabs are missing")
                evidence.append({"url": url, "final_url": str(response.url), "program_html": str(content),
                                 "membership_kind": "University program/intake information; reviewed separately",
                                 "programs": university_programs(response.text, str(response.url))})
            else:
                raise ValueError("Arista official careers journey changed or returned a challenge: " + url)
        self.result.metadata["official_journeys"] = evidence

    async def run(self):
        response = await self.http.get(self.company.careers_url)
        if urlsplit(str(response.url)).hostname != "www.arista.com":
            raise ValueError("Arista official careers entry redirected to another publisher")
        access_meta = None
        try:
            config = widget_config(response.text)
        except WidgetChallenge:
            # The challenge is an access observation, not a scope verdict: retry
            # the exact official entry through the stealth browser first.
            page = await access.fetch_with_stealth(str(response.url))
            access_meta = access.attempt_metadata(page, url=str(response.url))
            if page is None or not page.ok:
                self.result.metadata["access"] = access_meta
                raise
            response = access.as_response(page)
            config = widget_config(response.text)
            self.result.metadata["access"] = access_meta
        if any(config.get(k) for k in ("filter_departments", "filter_locations", "filter_languages")):
            raise ValueError("Arista official global widget unexpectedly filtered")
        script = await self.http.get(WIDGET_SCRIPT)
        if any(token not in script.text for token in ("var jobNumber = 100", "/job-api/public/search/widgets/", "vacancy.publicationId", "&offset=", "&limit=")):
            raise ValueError("Arista published widget pagination/link behavior changed")
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION, official_entry=str(response.url),
                                    listing_authority="Arista official embedded PUBLIC widget; exact published publication links only",
                                    official_widget=config, widget_script=WIDGET_SCRIPT, ats_board_enumeration_used=False,
                                    date_semantics="Native datePosted corroborates widget releasedDate milliseconds. og:updated_time is separately retained. Missing expiry is null.",
                                    description_semantics="Every author-written native description section in published order, including company, job, qualifications and additional information/benefits; attachments appended in full.")
        await self.inspect_journeys(BeautifulSoup(response.text, "lxml"))
        rows = await self.snapshot("initial", config)
        self.result.expected_total = len(rows)
        self.published_ids = {r["publicationId"] for r in rows}
        for row in rows:
            self.add(self.job({"listing": row, "verification_revision": self.VERIFICATION_REVISION},
                              title=row["vacancyName"].strip(), url=native_url(row), job_id=row["publicationId"],
                              requisition_id=row.get("refNumber"), department=row.get("department"), employer=row["companyName"],
                              workplace_type="remote" if row.get("locationRemote") else "hybrid" if row.get("locationHybrid") else "on_site"))
            if self.limit():
                break
        self.options.cached_jobs = {u: j for u, j in self.options.cached_jobs.items()
                                    if j.raw.get("verification_revision") == self.VERIFICATION_REVISION
                                    and any(j.raw.get("listing") == r for r in rows)}
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        if "sample_limit" in self.result.warnings:
            return self.result
        final = await self.snapshot("final", config)
        if [r["publicationId"] for r in rows] != [r["publicationId"] for r in final]:
            self.result.errors.append("arista_company_widget_initial_final_membership_changed")
        else:
            for job, row in zip(self.result.jobs, final):
                if job.raw["listing"] != row:
                    job.raw["initial_listing"] = job.raw["listing"]
                    job.raw["listing"] = row
                    try:
                        await self.detail(job)
                        self.result.validate_job(job)
                    except Exception as exc:
                        job.warnings.append("detail_fetch_failed")
                        self.result.errors.append(f"Arista changed exact publication {job.job_id}: {exc}")
                        self.result.validate_job(job)
        self.result.listing_complete = not self.result.errors
        return self.result
