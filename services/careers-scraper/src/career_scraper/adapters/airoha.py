"""Airoha's official recruiting form, native paginated table and full details."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


class Airoha(Adapter):
    VERIFICATION_REVISION = 'airoha.official-search-form-native-table-full-sections.1'

    async def listing(self):
        url, rows, pages, visited, complete = self.listing_entry, {}, [], set(), False
        expected_last, expected_page = None, 1
        while url:
            if url in visited:
                raise ValueError('Airoha native next-page link repeats a previous page')
            visited.add(url)
            if not self.page_guard():
                break
            response = await self.http.get(url)
            parsed = urlsplit(str(response.url))
            if parsed.hostname != 'careers.airoha.com' or parsed.path != '/Jobs':
                raise ValueError('Airoha native listing redirected away from its public jobs table')
            soup = BeautifulSoup(response.text, 'lxml')
            table, pager = soup.select_one('table.Rwd-Table'), soup.select_one('ul.pagination')
            if table is None or pager is None:
                raise ValueError('Airoha published listing table/pagination is missing; not an empty-job signal')
            current = pager.select_one('li.active')
            if not current or current.get_text(strip=True) != str(expected_page):
                raise ValueError('Airoha native active page does not match its next-page request')
            last_link = pager.select_one('li.PagedList-skipToLast a[href]')
            if last_link:
                observed_last = int(parse_qs(urlsplit(last_link['href']).query)['page'][0])
                if expected_last is None:
                    expected_last = observed_last
                elif expected_last != observed_last:
                    raise ValueError('Airoha page count changed during enumeration')
            labels = [n.get_text(' ', strip=True) for n in table.select('thead th')]
            if labels[:5] != ['職缺名稱','職務類別','工作地區','工作年資','教育程度']:
                raise ValueError('Airoha native public job columns changed')
            page_ids = []
            for tr in table.select('tbody tr'):
                a = tr.select_one('a[href*="/Jobs/Detail?"]')
                if not a:
                    raise ValueError('Airoha job row has no exact native detail URL')
                link = urljoin(str(response.url), a['href'])
                detail_url = urlsplit(link)
                identity = parse_qs(detail_url.query).get('sn', [''])[0]
                if detail_url.hostname != 'careers.airoha.com' or detail_url.path != '/Jobs/Detail' or str(UUID(identity)) != identity:
                    raise ValueError('Airoha listing contains an unexpected detail URL or invalid job UUID')
                if identity in rows:
                    raise ValueError('Airoha job UUID repeats across native listing pages')
                cells = tr.find_all('td', recursive=False)
                if len(cells) != len(labels):
                    raise ValueError('Airoha job row no longer matches its published columns')
                fields = {key:node.get_text(' ', strip=True) for key,node in zip(labels,cells) if key}
                rows[identity] = {'id':identity,'url':link,'title':fields['職缺名稱'],
                                  'fields':fields,'listing_url':str(response.url),'row_html':str(tr)}
                page_ids.append(identity)
                if self.options.max_jobs and len(rows) >= self.options.max_jobs:
                    self.result.warnings.append('sample_limit')
                    return rows, False
            if not page_ids:
                raise ValueError('Airoha native jobs page unexpectedly contains no rows')
            pages.append({'url':str(response.url), 'page':expected_page,'job_ids':page_ids})
            next_link = pager.select_one('li.PagedList-skipToNext:not(.disabled) a[href]')
            if next_link:
                url = urljoin(str(response.url), next_link['href'])
                next_url = urlsplit(url)
                if (next_url.hostname != 'careers.airoha.com' or next_url.path != '/Jobs'
                        or parse_qs(next_url.query).get('page') != [str(expected_page+1)]):
                    raise ValueError('Airoha next-page request skips or filters listing membership')
                expected_page += 1
            else:
                if expected_last is not None and expected_page != expected_last:
                    raise ValueError('Airoha pagination stopped before its advertised last page')
                if not pager.select_one('li.PagedList-skipToNext.disabled'):
                    raise ValueError('Airoha published next-page exhaustion state is missing')
                complete, url = True, None
        self.result.metadata.setdefault('native_enumerations', []).append(pages)
        return rows, complete

    async def detail(self, job):
        response = await self.http.get(job.url)
        parsed = urlsplit(str(response.url))
        if (parsed.hostname != 'careers.airoha.com' or parsed.path != '/Jobs/Detail'
                or parse_qs(parsed.query).get('sn') != [job.job_id]):
            raise ValueError('Airoha full job page redirected away from its exact listed UUID')
        soup = BeautifulSoup(response.text, 'lxml')
        heading = soup.select_one('.Detail-Title h1')
        if not heading or heading.get_text(' ', strip=True) != job.title:
            raise ValueError('Airoha detail title does not match its published job row')
        sections = {}
        for title in soup.select('h3.w3-fit-text'):
            label = title.get_text(' ', strip=True)
            if label in sections:
                raise ValueError('Airoha full description contains repeated section headings')
            sections[label] = str(title.parent)
        if set(sections) != {'工作說明','需求條件'}:
            raise ValueError('Airoha full work description or requirements section missing/changed')
        if not all(BeautifulSoup(value,'lxml').select_one('p') is not None
                   and BeautifulSoup(value,'lxml').select_one('p').get_text(' ',strip=True) for value in sections.values()):
            raise ValueError('Airoha full description section is empty')
        role = text_html(sections['工作說明']).replace('工作說明','',1)
        role = re.sub(r'【優先審閱官網投遞】\s*請至達發官方網站投遞個人履歷表，網址:https://careers\.airoha\.com/', '', role)
        # The inspected 2027 RDSS postings have two complete, concise Chinese
        # responsibilities. Character counts for English cannot judge those.
        # Strip the recruitment template first, then require actual role text.
        if len(re.findall(r'[\u3400-\u9fff]', role)) < 5 and len(re.findall(r'[A-Za-z]', role)) < 20:
            raise ValueError('Airoha native job has no substantive role beyond the application template')
        fields = {td['data-label']:td.get_text(' ', strip=True) for td in soup.select('table.Rwd-Table-Info td[data-label]')}
        if set(fields) != {'職務類別','工作地區','工作年資','教育程度','性質'}:
            raise ValueError('Airoha public job detail metadata columns changed')
        for label,value in job.raw['listing']['fields'].items():
            if label != '職缺名稱' and value != fields.get(label):
                raise ValueError('Airoha job detail metadata conflicts with its listing row: '+label)
        apply = next((a for a in soup.select('a[href]') if a.get_text(' ',strip=True)=='我要應徵'),None)
        if apply is None:
            raise ValueError('Airoha published exact-job application link is missing')
        job.description_html = '\n'.join(sections[label] for label in ('工作說明','需求條件'))
        job.description_method = 'airoha.native.full_work_and_requirements_sections'
        job.description_source_url = str(response.url)
        job.department, job.locations = fields['職務類別'], [fields['工作地區']]
        job.education, job.experience, job.employment_type = fields['教育程度'], fields['工作年資'], fields['性質']
        job.qualifications = text_html(sections['需求條件'])
        job.responsibilities = text_html(sections['工作說明'])
        job.apply_url = urljoin(str(response.url), apply['href'])
        if (urlsplit(job.apply_url).hostname != 'careers.airoha.com'
                or job.job_id not in parse_qs(urlsplit(job.apply_url).query).get('returnurl',[''])[0]):
            raise ValueError('Airoha application link does not preserve this exact job UUID')
        job.language = soup.find('html').get('lang') if soup.find('html') else None
        job.raw['detail'] = {'id':job.job_id,'title':heading.get_text(' ',strip=True),'sections':sections,
                             'fields':fields,'application_url':job.apply_url}
        return job

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text, 'lxml')
        forms = [form for form in soup.select('form[action]')
                 if urljoin(str(entry.url),form['action']) == 'https://careers.airoha.com/Jobs']
        if len(forms) != 1 or forms[0].get('method','get').lower() != 'get' or not forms[0].select_one('input[name="keyword"]'):
            raise ValueError('Airoha official careers page no longer exposes the inspected public job search form')
        if forms[0].select_one('input[name="keyword"]').get('value',''):
            raise ValueError('Airoha official form contains an unexpected keyword filter')
        self.listing_entry = forms[0]['action']+'?keyword='
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,
                                    official_entry=str(entry.url), official_search_form_action=forms[0]['action'],
                                    listing_authority='Company-owned recruiting portal linked by the official careers form, all filters empty')
        rows, complete = await self.listing()
        self.result.expected_total = len(rows)
        for identity,row in rows.items():
            code = re.match(r'^([A-Z]\d+)\s*-',row['title'])
            self.add(self.job({'listing':row},title=row['title'],job_id=identity,
                              requisition_id=code[1] if code else None,url=row['url'],employer='Airoha Technology'))
        old_cache = self.options.cached_jobs
        self.options.cached_jobs = {url:j for url,j in old_cache.items()
                                    if j.description_method == 'airoha.native.full_work_and_requirements_sections'
                                    and j.job_id in rows and j.title == rows[j.job_id]['title']
                                    and all(j.raw.get('detail',{}).get('fields',{}).get(label) == value
                                            for label,value in rows[j.job_id]['fields'].items() if label != '職缺名稱')}
        try:
            self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        finally:
            self.options.cached_jobs = old_cache
        if 'sample_limit' not in self.result.warnings:
            final, final_complete = await self.listing()
            if not final_complete or set(final) != set(rows):
                self.result.errors.append('airoha_native_listing_membership_changed')
            complete = complete and final_complete and set(final) == set(rows)
        self.result.metadata['company_published_job_ids'] = list(rows)
        self.result.metadata['count_basis'] = 'Native table rows over every actual next-page link, with disabled-next last-page exhaustion and final full membership reconciliation'
        self.result.listing_complete = complete and not self.result.errors and 'sample_limit' not in self.result.warnings
        return self.result
