"""Adobe's inspected native Phenom listing and full native job pages.

The Workday account/login link is not a listing authority. Listing teasers and
AI summaries are never substituted for the job page's full description field.
"""
import gzip
from hashlib import sha256
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import HTTP
from ..models import now, text_html
from ..structured import embedded_page_object as page_object


JOB_FIELDS = frozenset('jobId jobSeqNo reqId jobRequisitionId title description jobCode postedDate jobUpdatedDate jobPostingEndDate postingSiteType siteType type employmentType scheduledWeeklyHours positionWorkerType experienceLevel jobLevel category multi_category profession jobFamilyGroup jobFamily jobFamilyId jobProfile location multi_location standardised_multi_location city state country postalCode address latlong latitude longitude mapQueryLocation locationId remote applyUrl externalApply jobVisibility postingStatus visibilityType locale companyName refNum standardisedCountry standardisedState standardisedCity standardisedStateCode'.split())
LIST_FIELDS = frozenset('jobId jobSeqNo reqId title postedDate type category multi_category location multi_location multi_location_array city state country experienceLevel department jobVisibility applyUrl externalApply locale'.split())


class Adobe(Adapter):
    VERIFICATION_REVISION = 'adobe.native-phenom-listing-and-full-pages.1'

    def evidence(self, url, data, *, body=None):
        if not self.http.output:
            return
        encoded = json.dumps(data, ensure_ascii=False).encode()
        digest = sha256(encoded).hexdigest()
        target = self.http.output/'responses'/f'{digest}.gz'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(encoded))
        with (self.http.output/'requests.jsonl').open('a') as handle:
            handle.write(json.dumps({'url':url,'final_url':url,'method':'POST' if body else 'GET',
                                     'request_body':body,'status':200,'sha256':digest,'fetched_at':now(),
                                     'evidence_scope':'public job fields; unrelated personnel and AI summaries omitted'})+'\n')

    async def listing(self, api, offset):
        if not self.page_guard():
            raise ValueError('Adobe native listing page budget exhausted')
        body = {'lang':self.config['locale'],'deviceType':self.config['deviceType'],
                'country':self.config['country'],'pageName':self.config['pageName'],
                'ddoKey':'refineSearch','from':offset,'size':10,'global':True,'selected_fields':{},
                'all_fields':['city','state','category','type','orgFunction','country'],
                'clearAll':True,'keywords':'','subsearch':'','sortBy':'','jobs':True,'counts':True}
        response = await api.json(self.endpoint, method='POST', json=body, headers={'Referer':self.search_entry})
        value = response.get('refineSearch') if isinstance(response, dict) else None
        if (not isinstance(value,dict) or value.get('status')!=200 or not isinstance(value.get('totalHits'),int)
                or value['totalHits']<0 or not isinstance(value.get('data'),dict)
                or not isinstance(value['data'].get('jobs'),list)):
            raise ValueError('Adobe native search returned an error, counts-only result, or an unknown schema')
        jobs = value['data']['jobs']
        if len(jobs)>10 or value.get('hits')!=len(jobs):
            raise ValueError('Adobe native listing count/page size does not reconcile')
        selections = value['data'].get('ui_selections',{})
        if any(selections.values()):
            raise ValueError('Adobe Clear All request retained a category/location filter')
        clean = [{k:v for k,v in job.items() if k in LIST_FIELDS} for job in jobs]
        self.evidence(self.endpoint, {'refineSearch':{'status':200,'hits':len(clean),'totalHits':value['totalHits'],
                                                    'data':{'jobs':clean,'ui_selections':selections}}},body=body)
        return clean, value['totalHits']

    async def detail(self, api, job):
        response = await api.get(job.url)
        parsed = urlsplit(str(response.url))
        if parsed.hostname!='careers.adobe.com' or not re.match(r'/us/en/job/'+re.escape(job.job_id)+r'(?:/|$)',parsed.path):
            raise ValueError('Adobe posting redirected away from its exact native job ID')
        config = page_object(response.text,'var phApp = phApp || ')
        value = page_object(response.text,'phApp.ddo = ').get('jobDetail',{})
        data = value.get('data',{}).get('job') if isinstance(value,dict) else None
        if (config.get('refNum')!='ADOBUS' or not isinstance(data,dict) or value.get('status')!=200
                or data.get('jobId')!=job.job_id or data.get('jobSeqNo')!=job.raw['listing']['jobSeqNo']
                or data.get('refNum')!='ADOBUS' or data.get('title')!=job.title
                or not str(data.get('companyName','')).casefold().startswith('adobe')):
            raise ValueError('Adobe full native page does not match the company-listed posting identity')
        data = {key:value for key,value in data.items() if key in JOB_FIELDS}
        description = data.get('description')
        if not isinstance(description,str) or not description.strip():
            raise ValueError('Adobe full native description field is missing')
        role = re.split(r'\bAbout\s+Adobe\b',text_html(description),maxsplit=1,flags=re.I)[0]
        if len(role.strip())<100:
            job.warnings.append('description_not_full_field')
        self.evidence(str(response.url),{'jobDetail':{'status':200,'data':{'job':data}}})
        job.raw['detail'] = data
        job.raw['native_detail_url'] = job.url
        job.description_html = description
        job.description_method, job.description_source_url = 'adobe.native_phapp.jobDetail.description', str(response.url)
        job.locations = data.get('multi_location') or [{'location':data.get('location'),'country':data.get('country')}]
        job.employment_type = data.get('employmentType') or data.get('type')
        job.workplace_type = data.get('remote')
        job.department = data.get('multi_category') or data.get('category')
        job.experience = data.get('experienceLevel')
        job.requisition_id = data.get('jobRequisitionId') or data.get('reqId')
        job.date_posted = data.get('postedDate') or job.date_posted
        job.date_updated = data.get('jobUpdatedDate') or None
        job.valid_through = data.get('jobPostingEndDate') or None
        job.employer = data.get('companyName')
        job.language = data.get('locale')
        job.apply_url = urljoin(self.base,'apply?'+urlencode({'jobSeqNo':data['jobSeqNo']}))
        return job

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text,'lxml')
        link = next((a for a in soup.select('a[href]') if '/c/' in a['href']
                     and a.get_text(' ',strip=True).casefold()=='search jobs'),None)
        if link is None:
            raise ValueError('Adobe official entry has no inspected native Search Jobs category link')
        self.search_entry = urljoin(str(entry.url),link['href'])
        if urlsplit(self.search_entry).hostname!='careers.adobe.com':
            raise ValueError('Adobe Search Jobs left its inspected company-hosted listing')
        async with HTTP(None,delay=self.http.delay,timeout=self.http.client.timeout,retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            category = await api.get(self.search_entry)
            self.config = page_object(category.text,'var phApp = phApp || ')
            self.endpoint = self.config.get('widgetApiEndpoint')
            self.base = self.config.get('baseUrl','')
            if (self.config.get('refNum')!='ADOBUS' or self.endpoint!='https://careers.adobe.com/widgets'
                    or self.config.get('siteType')!='external' or self.base!='https://careers.adobe.com/us/en/'):
                raise ValueError('Adobe native company/search configuration changed')
            self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,
                                        official_entry=str(entry.url),native_search_entry=self.search_entry,
                                        listing_authority='Adobe native unfiltered Clear All search; not Workday account navigation')
            ids=set()
            offset=0
            while True:
                rows,total = await self.listing(api,offset)
                if self.result.expected_total is None:
                    self.result.expected_total=total
                elif total!=self.result.expected_total:
                    self.result.errors.append('adobe_native_total_changed_during_pagination')
                if not rows and len(ids)<self.result.expected_total:
                    self.result.errors.append('adobe_native_pagination_ended_early')
                    break
                for row in rows:
                    job_id=row.get('jobId')
                    if not job_id or job_id in ids or not row.get('jobSeqNo'):
                        raise ValueError('Adobe native listing ID missing or repeated across pages')
                    ids.add(job_id)
                    self.add(self.job({'listing':row},title=row['title'],job_id=job_id,
                                      url=urljoin(self.base,'job/'+job_id),date_posted=row.get('postedDate')))
                    if self.limit():
                        break
                if 'sample_limit' in self.result.warnings or len(ids)>=self.result.expected_total:
                    break
                offset+=len(rows)
            self.result.listing_complete=(not self.result.errors and len(ids)==self.result.expected_total
                                          and 'sample_limit' not in self.result.warnings)
            self.result.jobs=await self.hydrate(self.result.jobs,lambda job:self.detail(api,job))
            if 'sample_limit' not in self.result.warnings:
                final_ids=set()
                offset=0
                while len(final_ids)<self.result.expected_total:
                    rows,total=await self.listing(api,offset)
                    new={row['jobId'] for row in rows}
                    if not new or final_ids & new or total!=self.result.expected_total:
                        self.result.errors.append('adobe_native_final_enumeration_did_not_reconcile')
                        break
                    final_ids.update(new)
                    offset+=len(rows)
                if final_ids!=ids:
                    self.result.listing_complete=False
                    self.result.errors.append('adobe_native_listing_id_set_changed')
            self.result.metadata['company_published_job_ids']=sorted(ids)
        return self.result
