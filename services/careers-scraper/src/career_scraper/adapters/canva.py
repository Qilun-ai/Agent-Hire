"""Canva's inspected native 37-card HTML listing (20 + 17) and exact native role articles.

The native site embeds another role's JSON-LD in several detail pages; that
foreign metadata is never accepted as the requested job. Plain non-browser
request headers can make the edge return page one for a page-two request, so
this adapter sends standard browser headers on its own official listing and
detail requests and still refuses any response that does not identify the
requested page. There is no SmartRecruiters board enumeration here.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ENTRY = 'https://www.lifeatcanva.com/en/jobs/'
JOB_PATH = re.compile(r'^/en/jobs/(\d+)/[^/]+/$')
# The site's pagination serves page 1 for page 2 with minimal non-browser
# headers; a standard Chrome header returns the requested page. This only
# selects the site's normal HTML rendering for its own public pages.
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def normal(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def parse_listing(html, url, requested_page):
    soup = BeautifulSoup(html, 'lxml')
    results = soup.select_one('#js-job-search-results[data-results]')
    selected = soup.select_one('nav[aria-label="Pagination"] a[aria-current="page"]')
    if not results or not selected or normal(selected.get_text()) != str(requested_page):
        raise ValueError(f'Native Canva response does not identify requested page {requested_page}')
    total = int(results['data-results'])
    rows = []
    for card in results.select('.card-job[data-id]'):
        link = card.select_one('h2 a[href]')
        if not link:
            raise ValueError('Canva listing card has no exact native job link')
        target = urljoin(url, link['href'])
        parsed = urlsplit(target)
        match = JOB_PATH.fullmatch(parsed.path)
        if parsed.hostname != 'www.lifeatcanva.com' or not match or match[1] != card['data-id']:
            raise ValueError('Canva native card ID and URL disagree')
        labels = [normal(x.get_text(' ', strip=True)) for x in card.select('ul.job-meta li')]
        rows.append({'id': card['data-id'], 'title': normal(link.get_text(' ', strip=True)),
                     'url': target, 'location': labels[0] if labels else None,
                     'department': labels[1] if len(labels) > 1 else None})
    if len({r['id'] for r in rows}) != len(rows) or len(rows) > 20 or (total and not rows):
        raise ValueError('Canva page has duplicate, excessive or missing native cards')
    next_node = soup.select_one('nav[aria-label="Pagination"] a[rel~="next"]')
    next_url = urldefrag(urljoin(url, next_node['href']))[0] if next_node else None
    if next_url:
        parsed = urlsplit(next_url)
        if (parsed.hostname != 'www.lifeatcanva.com' or parsed.path != '/en/jobs/'
                or parse_qs(parsed.query).get('page') != [str(requested_page + 1)]):
            raise ValueError('Canva next page left the observed native unfiltered journey')
    return {'total': total, 'page': requested_page, 'rows': rows, 'next_url': next_url}


def parse_detail(job, html, url):
    parsed = urlsplit(url)
    match = JOB_PATH.fullmatch(parsed.path)
    soup = BeautifulSoup(html, 'lxml')
    holder = soup.select_one('#js-job-detail[data-id]')
    title = soup.select_one('h1')
    article = soup.select_one('#js-job-detail article.cms-content')
    apply = soup.select_one('#js-apply-external[href]')
    if (parsed.hostname != 'www.lifeatcanva.com' or not match or match[1] != job.job_id
            or not holder or holder['data-id'] != job.job_id or not title
            or normal(title.get_text(' ', strip=True)) != normal(job.title)
            or normal(holder.get('data-jobtitle')) != normal(job.title) or not article):
        raise ValueError('Canva exact native detail identity/title/article mismatch')
    target = urljoin(url, apply['href']) if apply else None
    if target:
        dest = urlsplit(target)
        if dest.hostname != 'jobs.smartrecruiters.com' or not dest.path.startswith('/Canva/'+job.job_id+'-'):
            raise ValueError('Canva published application link is not for the exact listed job')
    fields = {}
    for node in soup.select('.hero-job ul.job-meta > li'):
        label = node.find('p')
        if label:
            label_text = normal(label.get_text(' ', strip=True))
            values = list(node.stripped_strings)
            fields[label_text] = normal(' '.join(values[1:]))
    jsonld = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.get_text())
            jsonld.extend(value if isinstance(value, list) else [value])
        except (ValueError, TypeError):
            job.warnings.append('invalid_source_jsonld')
    same_role = [x for x in jsonld if isinstance(x, dict) and x.get('@type') == 'JobPosting'
                 and x.get('url') == job.url and normal(x.get('title')) == normal(job.title)]
    if jsonld and not same_role:
        job.warnings.append('foreign_job_jsonld_ignored')
    if same_role:
        value = same_role[0]
        identifier = value.get('identifier')
        job.requisition_id = identifier if isinstance(identifier, str) else None
        job.date_posted_raw = value.get('datePosted')
        job.date_posted = value.get('datePosted')
        job.valid_through = value.get('validThrough')
        job.salary = value.get('baseSalary')
    job.raw.update(native_detail={'headers': fields, 'jsonld': jsonld,
                                 'accepted_jsonld_identity': bool(same_role)},
                   attachments=[urljoin(url, a['href']) for a in article.select('a[href]')
                                if re.search(r'\.(pdf|docx?)(?:[?#]|$)', a['href'], re.I)])
    job.department = fields.get('Team', job.department)
    job.employment_type = fields.get('Schedule')
    job.workplace_type = fields.get('Location Type')
    job.employer = 'Canva'
    job.apply_url = target
    job.description_html = str(article)
    job.description_text = text_html(str(article))
    job.description_method = 'canva.native_identity_bound_article'
    job.description_source_url = url
    if job.raw['attachments']:
        job.warnings.append('role_attachment_not_yet_verified')
        job.description_unavailable = True
    return job


class Canva(Adapter):
    async def _detail(self, job):
        response = await self.http.get(job.url, headers=BROWSER_HEADERS)
        return parse_detail(job, response.text, str(response.url))

    async def run(self):
        if self.company.name != 'Canva' or self.source.url != ENTRY:
            raise ValueError('Canva must start at its official native careers URL')
        rows, seen, next_url, page = [], set(), ENTRY, 1
        started = time.perf_counter()
        self.result.metadata.update(official_entry_url=ENTRY, native_page_size=20,
                                    membership_authority='Exact native unfiltered careers cards and pagination',
                                    jsonld_policy='Accept metadata only when its URL and title match the requested role',
                                    listing_pages=[])
        while next_url and self.page_guard():
            try:
                response = await self.http.get(next_url, headers=BROWSER_HEADERS)
                current = parse_listing(response.text, str(response.url), page)
                if self.result.expected_total is not None and current['total'] != self.result.expected_total:
                    raise ValueError('Canva total changed during native page traversal')
                self.result.expected_total = current['total']
                if seen.intersection(r['id'] for r in current['rows']):
                    raise ValueError('Canva pagination repeated already collected job IDs')
                seen.update(r['id'] for r in current['rows'])
                rows.extend(current['rows'])
                self.result.metadata['listing_pages'].append(current)
                next_url = current['next_url']
                page += 1
            except Exception as exc:
                self.result.errors.append(f'Native listing {next_url}: {type(exc).__name__}: {exc}')
                break
        self.result.timings['native_listing_seconds'] = time.perf_counter() - started
        self.result.listing_complete = not next_url and len(rows) == self.result.expected_total
        if self.result.listing_complete:
            final_rows, final_url, final_page = [], ENTRY, 1
            final_started = time.perf_counter()
            try:
                while final_url and self.page_guard():
                    response = await self.http.get(final_url, headers=BROWSER_HEADERS)
                    current = parse_listing(response.text, str(response.url), final_page)
                    if current['total'] != self.result.expected_total:
                        raise ValueError('Canva final listing total changed')
                    final_rows.extend(current['rows'])
                    final_url, final_page = current['next_url'], final_page + 1
                if final_url or final_rows != rows:
                    raise ValueError('Canva final listing identity/order/fields changed or pass unfinished')
                self.result.metadata['final_listing_rows'] = final_rows
            except Exception as exc:
                self.result.listing_complete = False
                self.result.errors.append(f'Final native membership: {type(exc).__name__}: {exc}')
            self.result.timings['final_membership_seconds'] = time.perf_counter() - final_started
        jobs = [self.job({'listing': r}, title=r['title'], url=r['url'], job_id=r['id'],
                         department=r['department'], locations=[r['location']] if r['location'] else [])
                for r in rows]
        await self.hydrate(jobs, self._detail)
        for job in jobs:
            self.add(job)
        self.result.metadata['collected_ids'] = [r['id'] for r in rows]
        self.result.metadata['current_scope_unverified'] = not self.result.listing_complete
        self.result.finish()
        return self.result
