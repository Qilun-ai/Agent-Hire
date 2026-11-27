"""Arcesium's native ten-row UI backed by its own complete RSC Job Listing.

Membership and full public fields come only from the official careers response.
The exact Greenhouse URLs supplied there are checked as individual details; no
Greenhouse board, search, account or parent-company request is made.
"""
from copy import deepcopy
from hashlib import sha256
from html import unescape
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY = 'https://www.arcesium.com/careers'
PUBLIC_HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}
DETAIL_PATH = re.compile(r'^/arcesiumllc/jobs/([0-9]+)$')


def compact_text(value):
    """DOM text without inserting artificial spaces between inline elements."""
    return ' '.join(BeautifulSoup(value, 'lxml').get_text().split())


def decode_content(value):
    # The native payload escapes the complete HTML wrapper. Do not unescape
    # already-normal HTML, which could turn literal technical examples into tags.
    for _ in range(2):
        if not (re.match(r'^\s*&(?:amp;)*lt;(?:div|p|section|article)\b', value, re.I)
                and re.search(r'&(?:amp;)*lt;/(?:div|p|section|article)&(?:amp;)*gt;\s*$', value, re.I)):
            break
        value = unescape(value)
    if not BeautifulSoup(value, 'lxml').find(['div', 'p', 'section', 'article']):
        raise ValueError('Arcesium native full HTML description wrapper is missing')
    return value


def flight_records(soup):
    """Decode only JSON push arguments and byte-length RSC text records.

    Text records may contain newlines, colons, JSON and non-ASCII text, and may
    span multiple push arguments. No JavaScript is evaluated.
    """
    fragments = []
    for node in soup.select('script'):
        script = node.get_text().strip().rstrip(';')
        match = re.fullmatch(r'self\.__next_f\.push\((\[.*\])\)', script, re.S)
        if not match:
            continue
        value = json.loads(match[1])
        if len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
            fragments.append(value[1])
    return parse_flight_stream(''.join(fragments).encode('utf-8'))


def parse_flight_stream(stream):
    if not stream:
        raise ValueError('Arcesium official React flight data is missing')
    records, cursor = {}, 0
    while cursor < len(stream):
        match = re.match(rb'([0-9a-f]*):', stream[cursor:])
        if not match:
            raise ValueError('Arcesium React flight record boundary changed')
        identity = match[1].decode()
        cursor += match.end()
        if stream[cursor:cursor + 1] == b'T':
            comma = stream.index(b',', cursor)
            size = int(stream[cursor + 1:comma], 16)
            cursor = comma + 1
            value = stream[cursor:cursor + size].decode('utf-8')
            if len(stream[cursor:cursor + size]) != size:
                raise ValueError('Arcesium React flight text record is truncated')
            cursor += size
        else:
            end = stream.index(b'\n', cursor)
            raw = stream[cursor:end].decode('utf-8')
            cursor = end + 1
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw  # Imports/hints are not job data.
        if identity:
            if identity in records and records[identity] != value:
                raise ValueError('Arcesium React flight record is repeated with conflicting values')
            records[identity] = value
    return records


def _components(value):
    if isinstance(value, dict):
        component = value.get('component')
        if isinstance(component, dict) and component.get('name') == 'Job Listing':
            yield component.get('options', {}).get('mapped')
        for child in value.values():
            yield from _components(child)
    elif isinstance(value, list):
        for child in value:
            yield from _components(child)


def mapped_listing(mapped, records, visible=None):
    mapped = deepcopy(mapped)
    rows = mapped['jobs']
    references = {}
    ids = set()
    for row in rows:
        identity = row.get('id')
        link = urlsplit(row.get('absolute_url', ''))
        match = DETAIL_PATH.fullmatch(link.path)
        if (not isinstance(identity, int) or identity in ids or not row.get('title')
                or link.scheme != 'https' or link.hostname != 'job-boards.greenhouse.io'
                or not match or match[1] != str(identity) or link.query or link.fragment
                or row.get('company_name') != 'Arcesium LLC'):
            raise ValueError('Arcesium company-published posting identity/title/URL is invalid or repeated')
        ids.add(identity)
        content = row.get('content')
        if not isinstance(content, str):
            raise ValueError(f'Arcesium full native description is missing for {identity}')
        if re.fullmatch(r'\$[0-9a-f]+', content):
            references[str(identity)] = content
            content = records.get(content[1:])
        if not isinstance(content, str) or len(text_html(content)) < 100:
            raise ValueError(f'Arcesium full native description reference is missing for {identity}')
        role = BeautifulSoup(decode_content(content), 'lxml')
        for template in role.select('.content-intro,.content-conclusion'):
            template.decompose()
        if len(re.sub(r'\s', '', role.get_text())) < 100:
            raise ValueError(f'Arcesium native description contains only corporate template text for {identity}')
        row['content'] = content
    # SSR variants must match the actually rendered first page. The observed
    # server-action variant is independently reconciled to the rendered UI.
    if visible is not None:
        cards = visible.select('a[href*="job-boards.greenhouse.io/"]')
        if [a['href'] for a in cards] != [r['absolute_url'] for r in rows[:10]]:
            raise ValueError('Arcesium visible first page does not match its native Job Listing')
        for card, row in zip(cards, rows[:10]):
            title = card.select_one('p')
            if title is None or title.get_text(' ', strip=True) != row['title']:
                raise ValueError('Arcesium visible title disagrees with native job identity')
        display = re.search(r'Displaying\s+(\d+)\s+of\s+(\d+)\s+open positions', visible.get_text(' ', strip=True))
        if not display or (int(display[1]), int(display[2])) != (min(10, len(rows)), len(rows)):
            raise ValueError('Arcesium visible unfiltered count disagrees with native job membership')
    return rows, {'departments': mapped['departments'], 'offices': mapped['offices'],
                  'content_references': references, 'visible_page_ids': [r['id'] for r in rows[:10]] if visible is not None else None,
                  'ui_pages': [list(r['id'] for r in rows[i:i + 10]) for i in range(0, len(rows), 10)]}


def native_listing(html):
    soup = BeautifulSoup(html, 'lxml')
    visible = soup.select_one('#open-positions')
    if not visible:
        raise ValueError('Arcesium official open-positions section is missing')
    records = flight_records(soup)
    variants = {}
    for value in records.values():
        for mapped in _components(value):
            if isinstance(mapped, dict) and all(isinstance(mapped.get(k), list) for k in ('jobs', 'departments', 'offices')):
                variants[json.dumps(mapped, sort_keys=True, ensure_ascii=False)] = mapped
    if len(variants) != 1:
        raise ValueError('Arcesium official Job Listing dataset is missing or ambiguous')
    return mapped_listing(next(iter(variants.values())), records, visible)


def action_listing(content):
    records = parse_flight_stream(content)
    variants = {json.dumps(v, sort_keys=True, ensure_ascii=False): v for v in records.values()
                if isinstance(v, dict) and all(isinstance(v.get(k), list) for k in ('jobs', 'departments', 'offices'))}
    if len(variants) != 1:
        raise ValueError('Arcesium native JobListingMapper result is missing or ambiguous')
    return mapped_listing(next(iter(variants.values())), records)


def public_links(html):
    return [{'url': a['href'], 'text': a.get_text(' ', strip=True)}
            for a in BeautifulSoup(html, 'lxml').select('a[href]')]


def salary_statements(html):
    values = []
    for node in BeautifulSoup(html, 'lxml').select('p'):
        value = node.get_text(' ', strip=True)
        if re.search(r'(?:expected|anticipated).{0,50}(?:salary|compensation)', value, re.I) and re.search(r'[$£€]|\bUSD\b', value):
            if value not in values:
                values.append(value)
    return [{'text': value} for value in values] or None


class Arcesium(Adapter):
    VERIFICATION_REVISION = 'arcesium.own-site-rsc-full-job-listing.3'
    MAPPER_CHUNK = '/_next/static/chunks/5c87ebfa9324ac25.js'
    MAPPER_ACTION = '0056c88c707d94c27c34aa4250f59dbedc417eb597'

    async def listing(self):
        if not self.page_guard():
            raise ValueError('Arcesium native listing page budget exhausted')
        started = time.perf_counter()
        response = await self.http.get(ENTRY, headers=PUBLIC_HEADERS)
        if str(response.url) != ENTRY:
            raise ValueError('Arcesium native careers entry redirected; fresh journey review required')
        soup = BeautifulSoup(response.text, 'lxml')
        if soup.select_one('#open-positions') is not None:
            rows, evidence = native_listing(response.text)
            transport = {'kind': 'inline_ssr', 'method': 'GET', 'url': ENTRY}
        else:
            # The newly deployed native shell omits mapped data. Its actual
            # BuilderBlock calls the public declared JobListingMapper on mount.
            records = flight_records(soup)
            if not any(mapped is None for value in records.values() for mapped in _components(value)):
                raise ValueError('Arcesium official page has neither rendered listing nor declared unmapped Job Listing')
            scripts = [urljoin(ENTRY, node['src']) for node in soup.select('script[src]')
                       if urlsplit(urljoin(ENTRY, node['src'])).hostname == 'www.arcesium.com'
                       and urlsplit(node['src']).path == self.MAPPER_CHUNK]
            if len(scripts) != 1:
                raise ValueError('Arcesium inspected native JobListingMapper script changed; individual review required')
            if not getattr(self, '_mapper_verified', False):
                if not self.page_guard():
                    raise ValueError('Arcesium native mapper script page budget exhausted')
                script = await self.http.get(scripts[0], headers=PUBLIC_HEADERS)
                declaration = f'createServerReference)("{self.MAPPER_ACTION}"'
                if declaration not in script.text or '"JobListingMapper"' not in script.text:
                    raise ValueError('Arcesium native public mapper declaration is no longer the inspected function')
                self._mapper_verified = True
                self.result.metadata['native_mapper_script'] = scripts[0]
            if not self.page_guard():
                raise ValueError('Arcesium native mapper request page budget exhausted')
            headers = {**PUBLIC_HEADERS, 'Next-Action': self.MAPPER_ACTION,
                       'Content-Type': 'text/plain;charset=UTF-8', 'Accept': 'text/x-component',
                       'Origin': 'https://www.arcesium.com', 'Referer': ENTRY}
            mapped_response = await self.http.request('POST', ENTRY, headers=headers, json=[{}])
            if str(mapped_response.url) != ENTRY or 'text/x-component' not in mapped_response.headers.get('content-type', ''):
                raise ValueError('Arcesium native mapper response changed or redirected')
            rows, evidence = action_listing(mapped_response.content)
            transport = {'kind': 'own_site_JobListingMapper', 'method': 'POST', 'url': ENTRY,
                         'next_action': self.MAPPER_ACTION, 'request_body': [{}],
                         'authority': 'Own native Job Listing component and its shipped public server-reference declaration'}
        self.result.metadata.setdefault('native_listing_transports', []).append(transport)
        self.result.timings['native_listing_seconds'] = self.result.timings.get('native_listing_seconds', 0) + time.perf_counter() - started
        return rows, evidence

    async def exact_detail_check(self, row):
        url = row['absolute_url']
        if url not in self.published_urls:
            raise ValueError('Arcesium exact detail URL was not exposed by its official native listing')
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f'Arcesium exact job redirected away from its published URL: {url}')
        soup = BeautifulSoup(response.text, 'lxml')
        title, location, body = soup.select_one('h1'), soup.select_one('.job__location'), soup.select_one('.job__description.body')
        if (title is None or title.get_text(' ', strip=True) != row['title']
                or location is None or location.get_text(' ', strip=True) != row.get('location', {}).get('name')
                or body is None):
            raise ValueError(f'Arcesium exact title/location/full-body identity mismatch: {url}')
        native = decode_content(row['content'])
        if compact_text(native) != compact_text(str(body)):
            raise ValueError(f'Arcesium own-site and exact published job full bodies disagree: {url}')
        return {'job_id': str(row['id']), 'url': url, 'title': row['title'],
                'full_body_text_sha256': sha256(compact_text(str(body)).encode()).hexdigest(),
                'matched_native_full_body': True}

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION, official_entry=ENTRY,
            listing_authority='Arcesium own careers Job Listing mapped.jobs; no standalone ATS membership',
            pagination='Native client-side ten-row slices over complete unfiltered own-site job array',
            filters={'search': '', 'country_region': 'All', 'department': 'All'},
            date_semantics='first_published is public posting timestamp; updated_at is source update; null application_deadline remains null',
            ats_board_requests=0, independent_rendered_browser_review='separate company review artifact',
        )
        rows, evidence = await self.listing()
        self.result.expected_total = len(rows)
        self.published_urls = {row['absolute_url'] for row in rows}
        self.result.metadata.update(company_published_job_ids=[row['id'] for row in rows],
                                    ui_page_ids=evidence['ui_pages'],
                                    public_filter_taxonomy={'departments': evidence['departments'], 'offices': evidence['offices']})
        started = time.perf_counter()
        for row in rows:
            html = decode_content(row['content'])
            raw = {'native_job': row, 'native_content_reference': evidence['content_references'].get(str(row['id'])),
                   'description_links': public_links(html)}
            self.add(self.job(
                raw, title=row['title'], job_id=row['id'], requisition_id=row.get('requisition_id'),
                url=row['absolute_url'], apply_url=row['absolute_url'], description=html,
                method='arcesium.official_careers_rsc.complete_content', description_url=ENTRY,
                locations=[row['location']['name']] if row.get('location', {}).get('name') else [],
                department=[d['name'] for d in row.get('departments', [])], employer=row.get('company_name'),
                date_posted=row.get('first_published'), date_posted_raw=row.get('first_published'),
                date_updated=row.get('updated_at'), valid_through=row.get('application_deadline'),
                salary=salary_statements(html), language=row.get('language'),
            ))
            if self.limit():
                break
        self.result.timings['native_detail_extraction_seconds'] = time.perf_counter() - started
        # Both current first/last full job pages were independently inspected.
        # Re-select only current company-published members after normal turnover;
        # never request a removed historical role as a current sample.
        started = time.perf_counter()
        checks = []
        for index in (dict.fromkeys([0, len(rows) - 1]) if rows else []):
            if index >= 0:
                try:
                    checks.append(await self.exact_detail_check(rows[index]))
                except Exception as exc:
                    self.result.errors.append(f'Arcesium exact public detail: {type(exc).__name__}: {exc}')
        self.result.metadata['exact_detail_checks'] = checks
        self.result.timings['detail_phase_seconds'] = time.perf_counter() - started
        if 'sample_limit' not in self.result.warnings:
            final, final_evidence = await self.listing()
            if [(r['id'], r['title'], r['absolute_url']) for r in final] != [(r['id'], r['title'], r['absolute_url']) for r in rows]:
                self.result.errors.append('arcesium_official_final_membership_identity_changed')
            # Keep the full final fields authoritative; a changed body/date is a
            # visible unresolved snapshot drift, never silently certified.
            if final != rows:
                self.result.errors.append('arcesium_official_final_public_fields_changed')
            self.result.metadata['final_ui_page_ids'] = final_evidence['ui_pages']
        self.result.listing_complete = not self.result.errors and 'sample_limit' not in self.result.warnings
        return self.result
