from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..discovery import DETAIL_LINK
from ..models import Job


def job_postings(html):
    soup = BeautifulSoup(html, "lxml")
    found = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if "JobPosting" in ([kind] if isinstance(kind, str) else kind):
                found.append(value)
            else:
                for item in value.values():
                    if isinstance(item, (list, dict)):
                        visit(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    return found


def from_schema(company, source, row, url):
    identifier = row.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    if isinstance(identifier, list):
        identifier = next((x.get("value") for x in identifier if isinstance(x, dict) and x.get("value")), None)
    locations = row.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    return Job(company=company, source=source, title=row.get("title", ""),
        url=urljoin(url, row.get("url") or url), job_id=str(identifier) if identifier is not None else None,
        description_html=row.get("description", ""), description_method="schema.org.JobPosting.description",
        description_source_url=url, date_posted=row.get("datePosted"),
        date_posted_raw=row.get("datePosted"), valid_through=row.get("validThrough"),
        locations=locations, employment_type=row.get("employmentType"),
        workplace_type=row.get("jobLocationType"), salary=row.get("baseSalary"),
        qualifications=row.get("qualifications"), responsibilities=row.get("responsibilities"),
        experience=row.get("experienceRequirements"), education=row.get("educationRequirements"),
        employer=row.get("hiringOrganization"), raw=row)


class HTML(Adapter):
    """Structured data crawler; never mistakes an exhausted link queue for proof of all jobs."""

    async def run(self):
        config = self.source.options
        queue, seen = [self.source.url], set()
        listing_pages, detail_candidates = 0, 0
        while queue and self.page_guard():
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                response = await self.http.get(url)
                final, html = str(response.url), response.text
                if self.options.browser:
                    from ..browser import render
                    rendered = await render(final, self.http.output)
                    final, html = rendered["url"], rendered["html"]
                rows = job_postings(html)
                for row in rows:
                    self.add(from_schema(self.company.name, self.source.adapter, row, final))
                    if self.limit():
                        return self.result
                soup = BeautifulSoup(html, "lxml")
                link_base = urljoin(final, soup.find("base", href=True)["href"]) if soup.find("base", href=True) else final
                # Explicit, company-specific selectors are allowed; generic main/body isn't a job description.
                selector = config.get("description_selector")
                if not rows and selector and (node := soup.select_one(selector)):
                    title_node = soup.select_one(config.get("title_selector", "h1"))
                    self.add(self.job({"html_selector": selector}, title=title_node.get_text(" ", strip=True) if title_node else "",
                        url=final, description=str(node), method="company.dom_selector", description_url=final))
                    if self.limit():
                        return self.result
                if rows:
                    # Do not follow recommendations on detail pages as listing pagination.
                    continue
                listing_pages += 1
                count_selector = config.get("total_selector")
                if count_selector and (node := soup.select_one(count_selector)):
                    match = re.search(r"[\d,]+", node.get_text())
                    if match:
                        self.result.expected_total = int(match.group().replace(",", ""))
                for anchor in soup.select(config.get("job_link_selector", "a[href]")):
                    href = anchor.get("href")
                    if not href:
                        continue
                    target = urljoin(link_base, href)
                    parsed = urlsplit(target)
                    if parsed.scheme not in {"https", "http"}:
                        continue
                    label = anchor.get_text(" ", strip=True).lower()
                    rel = anchor.get("rel", [])
                    is_next = "next" in rel or label in {"next", "next page", "load more", "show more jobs"}
                    is_detail = bool(DETAIL_LINK.search(parsed.path))
                    if config.get("job_link_pattern"):
                        is_detail = bool(re.search(config["job_link_pattern"], target))
                    if is_detail or is_next:
                        target = urlunsplit(parsed._replace(fragment=""))
                        if target not in seen and target not in queue:
                            queue.append(target)
                            detail_candidates += int(is_detail)
            except Exception as exc:
                self.result.errors.append(f"{url}: {type(exc).__name__}: {exc}")
        if queue:
            self.result.errors.append("crawl_budget_exhausted")
        # Only an explicitly configured, checked total can certify custom HTML enumeration.
        self.result.listing_complete = bool(
            self.result.expected_total is not None
            and len(self.result.jobs) == self.result.expected_total
            and not queue and not self.result.errors
        )
        if not self.result.listing_complete:
            self.result.warnings.append("custom_site_total_not_verified")
        if not self.result.jobs:
            self.result.errors.append("no_job_structured_data_found; custom adapter or browser inspection required")
        return self.result
