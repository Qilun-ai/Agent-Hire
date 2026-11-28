"""Airbnb's native vacancies and the contractor portal linked by that listing.

Greenhouse's application embed is not a job-listing authority. The native
FacetWP pages determine employee membership; the explicitly linked Magnit
community determines current contractor membership. No ATS account search.
"""
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from bs4 import BeautifulSoup

from .base import Adapter
from ..structured import embedded_page_object


CONTRACT_FIELDS = frozenset('id title description type durationText publishedOrCreatedTime status closedAt startDate endDate isRemote workplaceType workTypeLabel location payRangeMin payRangeMax payRangeCurrency payRangeUnit payRangeNotes benefitsInformation skills'.split())


def title_identity(value):
    # WordPress typography converts the source's ASCII separator into an en dash.
    # Only typographic dash/whitespace equivalence is permitted, never fuzzy titles.
    return re.sub(r'\s+', ' ', re.sub('[\u2010-\u2015\u2212]', '-', unicodedata.normalize('NFC', value))).strip()


class Airbnb(Adapter):
    VERIFICATION_REVISION = 'airbnb.native-post-facetwp-and-officially-linked-contracts.2'
    BASE = 'https://careers.airbnb.com/positions/'
    CONTRACT_ROOT = 'https://magnit-airbnb.talentnet.community/'
    CONTRACT_API = 'https://talentnet-api-v6.v6-prod-use1.talentnet.community'

    async def employee_listing(self):
        rows, counts, issues, contract_links = {}, [], [], set()
        page, expected = 1, None
        while True:
            if not self.page_guard():
                break
            facets = {key:[] for key in ('jobs_total','departments','where_you_work','workplace_type','jobs_pager','jobs_reset','jobs_sort','jobs_pagination')}
            facets['search_input'] = ''
            body = {'action':'facetwp_refresh', 'data':{'facets':facets, 'frozen_facets':{},
                    'http_params':{'get':{'_paged':str(page)} if page > 1 else [], 'uri':'positions','url_vars':[], 'lang':'en'},
                    'template':'wp', 'extras':{'sort':'default'}, 'soft_refresh':1,
                    'is_bfcache':0, 'first_load':0, 'paged':page}}
            # The UI POSTs to its current native page. GET pages are independently
            # cached snapshots with different totals and overlapping memberships.
            response = await self.http.request('POST', self.BASE, params={'_paged':page} if page > 1 else {}, json=body)
            if urlsplit(str(response.url)).hostname != 'careers.airbnb.com' or urlsplit(str(response.url)).path.rstrip('/') != '/positions':
                raise ValueError('Airbnb listing redirected away from its native positions page')
            state = response.json()
            pager = state.get('settings', {}).get('pager', {})
            total, pages = int(pager['total_rows']), int(pager['total_pages'])
            if total < 0 or pages < 0:
                raise ValueError('Airbnb listing counts are invalid')
            counts.append(total)
            if expected is None:
                expected = total
            elif total != expected:
                issues.append('native_total_changed_during_pagination')
            if int(pager.get('page', 0)) != page or not isinstance(state.get('template'), str):
                raise ValueError('Airbnb native pagination response does not match its requested page')
            soup = BeautifulSoup(state['template'], 'lxml')
            new = 0
            for a in soup.select('a[href]'):
                url = urljoin(self.BASE, a['href'])
                if url.rstrip('/') == self.CONTRACT_ROOT.rstrip('/'):
                    contract_links.add(self.CONTRACT_ROOT)
                match = re.fullmatch(r'https://careers\.airbnb\.com/positions/(\d+)/?', url)
                if not match:
                    continue
                identity = match[1]
                if identity in rows:
                    issues.append('native_id_repeated_across_pages')
                    continue
                card = a.find_parent('li')
                department, workplace, locations = None, None, []
                if card:
                    columns = card.find_all('div', recursive=False)
                    if columns:
                        meta = columns[0].find('div', recursive=False)
                        labels = [n.get_text(' ', strip=True) for n in meta.find_all('span', recursive=False)] if meta else []
                        department = labels[0] if labels else None
                        workplace = labels[-1] if len(labels) > 2 else None
                    if len(columns) > 1:
                        locations = [n.get_text(' ', strip=True) for n in columns[-1].find_all('span', recursive=False)]
                rows[identity] = {'id':identity, 'title':a.get_text(' ', strip=True), 'url':url,
                                  'department':department, 'workplace_label':workplace, 'locations':locations,
                                  'listing_url':str(response.url), 'card_html':str(card) if card else str(a)}
                new += 1
                if self.options.max_jobs and len(rows) >= self.options.max_jobs:
                    self.result.warnings.append('sample_limit')
                    return rows, expected, False, contract_links
            if page >= pages:
                break
            if not new:
                issues.append('native_pagination_ended_early')
                break
            page += 1
        if len(rows) != expected:
            issues.append('native_pagination_count_mismatch')
        self.result.metadata.setdefault('employee_enumerations', []).append(
            {'expected_total':expected, 'observed_page_totals':counts, 'job_ids':list(rows), 'issues':issues})
        return rows, expected, not issues, contract_links

    async def employee_detail(self, job):
        response = await self.http.get(job.url)
        if str(response.url).rstrip('/') != job.url.rstrip('/'):
            raise ValueError('Airbnb native posting redirected away from its exact listed job URL')
        soup = BeautifulSoup(response.text, 'lxml')
        title, node = soup.select_one('h1'), soup.select_one('#job-detail-panel')
        actual_title = title.get_text(' ', strip=True) if title else ''
        if not title or title_identity(actual_title) != title_identity(job.title) or node is None:
            raise ValueError('Airbnb native full panel/title does not match its listed posting')
        form = soup.select_one('#job-application-panel')
        if form and form.get('data-job-id') != job.job_id:
            raise ValueError('Airbnb application panel belongs to a different posting ID')
        job.description_html = str(node)
        job.description_method = 'airbnb.native.job-detail-panel'
        job.description_source_url = str(response.url)
        job.locations = [n.get_text(' ', strip=True) for n in soup.select('.job-page-banner .offices > span')] or job.locations
        job.salary = [{'label':n.select_one('.title').get_text(' ', strip=True) if n.select_one('.title') else None,
                       'text':n.select_one('.pay-range').get_text(' ', strip=True)} for n in node.select('.pay-input') if n.select_one('.pay-range')]
        job.apply_url = job.url if form else None  # The observed Apply Now control opens the form on this same page.
        job.raw['detail'] = {'id':job.job_id, 'title':actual_title, 'description_html':str(node),
                             'locations':job.locations, 'salary':job.salary, 'application_form_on_page':bool(form)}
        # WordPress WebPage.datePublished is not a JobPosting publication date.
        return job

    async def contract_listing(self):
        rows, total, page, expected_pages = {}, None, 1, None
        while True:
            if not self.page_guard():
                return rows, total, False
            response = await self.http.json(self.CONTRACT_API+'/api/community/jobs/search',
                                            params={'limit':12, 'page':page}, headers=self.contract_headers)
            data, meta = response.get('data'), response.get('meta', {})
            count, pages = meta.get('totalCount'), meta.get('pageCount')
            if (not isinstance(data, list) or not isinstance(count, int) or not isinstance(pages, int)
                    or count < 0 or pages < 0 or len(data) > 12):
                raise ValueError('Airbnb linked contractor listing schema/counts changed')
            if total is None:
                total, expected_pages = count, pages
            elif total != count or expected_pages != pages:
                raise ValueError('Airbnb linked contractor count changed during pagination')
            for row in data:
                identity = row.get('id')
                if not identity or str(UUID(identity)) != identity or identity in rows:
                    raise ValueError('Airbnb contractor ID missing, invalid or repeated across pages')
                if row.get('status') != 'published' or row.get('closedAt') or not row.get('title', {}).get('name'):
                    raise ValueError('Airbnb current contractor listing returned a closed/unidentified posting')
                rows[identity] = {k:v for k,v in row.items() if k in CONTRACT_FIELDS}
            if page >= pages:
                break
            if not data:
                raise ValueError('Airbnb contractor pagination ended early')
            page += 1
        return rows, total, len(rows) == total

    async def run(self):
        entry = await self.http.get(self.BASE)
        state = embedded_page_object(entry.text, 'window.FWP_JSON = ')
        if state.get('ajaxurl') != 'https://careers.airbnb.com/wp-json/facetwp/v1/refresh':
            raise ValueError('Airbnb official native listing configuration changed')
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,
                                    official_entry=self.BASE,
                                    listing_authority='Native FacetWP employee listings plus the Magnit community explicitly linked there')
        root = BeautifulSoup(entry.text, 'lxml')
        contract_links = {self.CONTRACT_ROOT for a in root.select('a[href]') if urljoin(self.BASE,a['href']).rstrip('/') == self.CONTRACT_ROOT.rstrip('/')}
        existing, previous, previous_ok, stable, attempted = {}, None, False, False, set()
        started = time.perf_counter()
        for _ in range(3):
            rows, count, consistent, links = await self.employee_listing()
            contract_links.update(links)
            # Reuse fetched details by exact ID+title, only fetch genuinely new jobs.
            current = {}
            for identity, row in rows.items():
                job = existing.get(identity)
                if job is None or job.title != row['title']:
                    job = self.job({'kind':'employee', 'listing':row}, title=row['title'], job_id=identity,
                                   url=row['url'], employer='Airbnb', locations=row['locations'],
                                   department=row['department'], workplace_type=row['workplace_label'])
                current[identity] = job
            existing = current
            self.result.jobs = list(existing.values())
            self.result.expected_total = count
            stable = consistent and previous_ok and set(rows) == previous
            to_fetch = [j for j in self.result.jobs if not j.description_verified and (j.job_id,j.title) not in attempted]
            attempted.update((j.job_id,j.title) for j in to_fetch)
            # Do not trust legacy cached descriptions lacking this flow's identity checks.
            old_cache = self.options.cached_jobs
            self.options.cached_jobs = {u:j for u,j in old_cache.items()
                                        if j.description_method == 'airbnb.native.job-detail-panel'
                                        and j.raw.get('detail', {}).get('id') == j.job_id
                                        and j.job_id in existing and title_identity(j.title) == title_identity(existing[j.job_id].title)}
            try:
                if to_fetch:
                    await self.hydrate(to_fetch, self.employee_detail)
            finally:
                self.options.cached_jobs = old_cache
            existing.update({j.job_id:j for j in to_fetch})
            for identity, job in existing.items():
                job.raw['listing'] = rows[identity]
            self.result.jobs = list(existing.values())
            if stable:
                break
            previous, previous_ok = set(rows), consistent
            if 'sample_limit' in self.result.warnings:
                break
        self.result.timings['employee_collection_seconds'] = time.perf_counter()-started
        self.result.metadata['employee_job_ids'] = sorted(existing)
        self.result.metadata['employee_total'] = len(existing)
        self.result.metadata['employee_membership_reconciled'] = stable
        if not stable and 'sample_limit' not in self.result.warnings:
            self.result.errors.append('airbnb_native_membership_not_stable_after_bounded_rechecks')

        contracts_ok = True
        if contract_links and 'sample_limit' not in self.result.warnings:
            self.result.metadata['official_contractor_link'] = self.CONTRACT_ROOT
            portal = await self.http.get(self.CONTRACT_ROOT)
            if urlsplit(str(portal.url)).hostname != 'magnit-airbnb.talentnet.community':
                raise ValueError('Airbnb linked contractor portal redirected to another tenant')
            config = await self.http.json(urljoin(self.CONTRACT_ROOT, 'prod/magnit-airbnb.json'))
            if (config.get('tenant') != 'magnit_airbnb' or config.get('displaySlug') != 'magnit-airbnb'
                    or config.get('endpoints', {}).get('api') != self.CONTRACT_API):
                raise ValueError('Airbnb linked public contractor bootstrap tenant/API changed')
            self.contract_headers = {'x-tenant':config['tenant'], 'x-spa-type':'community',
                                     'Accept-Language':'en', 'Origin':self.CONTRACT_ROOT.rstrip('/')}
            started = time.perf_counter()
            rows, total, contracts_ok = await self.contract_listing()
            # Full description is published inline in the actual listing response;
            # validate the inspected first/last exact detail route, without an ATS search.
            for identity in dict.fromkeys([next(iter(rows), None), next(reversed(rows), None)]):
                if identity is None:
                    continue
                detail = (await self.http.json(self.CONTRACT_API+'/api/community/job/'+identity,
                                              headers=self.contract_headers)).get('data')
                if (not isinstance(detail, dict) or detail.get('id') != identity
                        or detail.get('description') != rows[identity].get('description')
                        or detail.get('title', {}).get('name') != rows[identity]['title']['name']):
                    raise ValueError('Airbnb contractor listing and exact detail body/identity differ')
            for identity, row in rows.items():
                url = self.CONTRACT_ROOT+'jobs/'+identity
                self.add(self.job({'kind':'contractor', 'detail':row, 'client':'Airbnb',
                                   'official_contractor_link':self.CONTRACT_ROOT, 'public_tenant':config['tenant']},
                                  title=row['title']['name'], job_id=identity, url=url, apply_url=url,
                                  description=row.get('description'), method='airbnb.linked-magnit.full_description',
                                  description_url=self.CONTRACT_API+'/api/community/jobs/search',
                                  employer='Magnit', employment_type=row.get('type'), workplace_type=row.get('workplaceType'),
                                  locations=[row['location']] if row.get('location') else [],
                                  date_posted=row.get('publishedOrCreatedTime'),
                                  date_posted_raw={'publishedOrCreatedTime':row.get('publishedOrCreatedTime')},
                                  salary={k:v for k,v in row.items() if k.startswith('payRange')}))
            final, final_total, final_ok = await self.contract_listing()
            contracts_ok = contracts_ok and final_ok and total == final_total and set(rows) == set(final)
            if not contracts_ok:
                self.result.errors.append('airbnb_linked_contractor_membership_did_not_reconcile')
            self.result.expected_total += total
            self.result.metadata.update(contractor_total=total, contractor_job_ids=sorted(rows),
                                        contractor_membership_reconciled=contracts_ok,
                                        contractor_description_detail_checks=list(dict.fromkeys([next(iter(rows),None),next(reversed(rows),None)])))
            self.result.timings['contractor_collection_seconds'] = time.perf_counter()-started
        self.result.listing_complete = stable and contracts_ok and not self.result.errors and 'sample_limit' not in self.result.warnings
        return self.result
