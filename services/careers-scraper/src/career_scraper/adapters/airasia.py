"""AirAsia's own careers entry, global brand listing and exact native job pages."""
import gzip
from hashlib import sha256
import json
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import HTTP
from ..models import now, text_html
from ..structured import embedded_page_object


PUBLIC_FIELDS = frozenset('jobId jobSeqNo reqId jobRequisitionId title description descriptionHeader descriptionFooter jobCode postedDate jobUpdatedDate siteType type employmentType experienceLevel jobLevel seniority category multi_category profession jobFamily jobFamilyId jobProfile location multi_location city state country postalCode address latlong latitude longitude locationId mapQueryLocation applyUrl externalApply jobVisibility postingStatus visibilityType locale companyName refNum brand subBrand companyBrand businessUnit office allstarsType hiringType internship jobType'.split())
LIST_FIELDS = frozenset('jobId jobSeqNo reqId title postedDate type category multi_category location multi_location multi_location_array city state country department jobVisibility applyUrl externalApply locale brand subBrand companyBrand businessUnit allstarsType hiringType internship jobType'.split())


class AirAsia(Adapter):
    VERIFICATION_REVISION = 'airasia.official-entry-global-native-listing.1'

    def evidence(self, url, data, body=None):
        if not self.http.output:
            return
        payload=json.dumps(data,ensure_ascii=False).encode()
        digest=sha256(payload).hexdigest()
        path=self.http.output/'responses'/f'{digest}.gz'
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            path.write_bytes(gzip.compress(payload))
        with (self.http.output/'requests.jsonl').open('a') as handle:
            handle.write(json.dumps({'url':url,'final_url':url,'method':'POST' if body else 'GET','request_body':body,
                                     'status':200,'sha256':digest,'fetched_at':now(),
                                     'evidence_scope':'company-published job fields; generated summaries/personnel omitted'})+'\n')

    async def listing(self, api, start):
        if not self.page_guard():
            raise ValueError('AirAsia public listing pagination budget exhausted')
        body={'lang':self.config['locale'],'country':self.config['country'],'deviceType':self.config['deviceType'],
              'pageName':self.config['pageName'],'ddoKey':'refineSearch','from':start,'size':10,'jobs':True,'counts':True,
              'global':True,'selected_fields':{},'all_fields':['city','state','category','type','orgFunction','country'],
              'clearAll':True,'keywords':'','subsearch':'','sortBy':''}
        response=await api.json(self.endpoint,method='POST',json=body,headers={'Referer':self.search_url})
        value=response.get('refineSearch') if isinstance(response,dict) else None
        if (not isinstance(value,dict) or value.get('status')!=200 or not isinstance(value.get('totalHits'),int)
                or value['totalHits']<0 or not isinstance(value.get('data'),dict)
                or not isinstance(value['data'].get('jobs'),list)):
            raise ValueError('AirAsia public listing returned an error or an unknown schema')
        rows=value['data']['jobs']
        if len(rows)>10 or value.get('hits')!=len(rows) or any((value['data'].get('ui_selections') or {}).values()):
            raise ValueError('AirAsia page count/filter state does not match the inspected all-jobs request')
        clean=[{key:item for key,item in row.items() if key in LIST_FIELDS} for row in rows]
        self.evidence(self.endpoint,{'refineSearch':{'status':200,'hits':len(clean),'totalHits':value['totalHits'],
                                                   'data':{'jobs':clean}}},body)
        return clean,value['totalHits']

    async def detail(self, api, job):
        response=await api.get(job.url)
        parsed=urlsplit(str(response.url))
        if parsed.hostname!='mycareer.airasia.com' or parsed.path.split('/job/')[-1].split('/')[0]!=job.job_id:
            raise ValueError('AirAsia native detail redirected away from its exact listed job ID')
        config=embedded_page_object(response.text,'var phApp = phApp || ')
        value=embedded_page_object(response.text,'phApp.ddo = ').get('jobDetail',{})
        data=value.get('data',{}).get('job') if isinstance(value,dict) else None
        if (not isinstance(data,dict) or value.get('status')!=200 or config.get('refNum')!='AIRAGLOBAL'
                or data.get('refNum')!='AIRAGLOBAL' or data.get('jobId')!=job.job_id
                or data.get('jobSeqNo')!=job.raw['listing']['jobSeqNo'] or data.get('title')!=job.title):
            raise ValueError('AirAsia detail identity does not match its company-published listing')
        data={key:item for key,item in data.items() if key in PUBLIC_FIELDS}
        body=data.get('description')
        if not isinstance(body,str) or len(text_html(body).strip())<100:
            raise ValueError('AirAsia full native description is missing or requires review')
        self.evidence(str(response.url),{'jobDetail':{'status':200,'data':{'job':data}}})
        job.raw['detail']=data
        job.raw['native_detail_url']=job.url
        job.description_html=body
        job.description_method='airasia.native_phapp.jobDetail.description'
        job.description_source_url=str(response.url)
        job.requisition_id=data.get('jobRequisitionId') or data.get('reqId')
        job.locations=data.get('multi_location') or [{'location':data.get('location'),'country':data.get('country')}]
        job.employer=data.get('companyName')
        job.department=data.get('category')
        job.employment_type=data.get('employmentType') or data.get('type')
        job.experience=data.get('seniority') or data.get('experienceLevel')
        job.date_posted=data.get('postedDate') or job.date_posted
        job.date_updated=data.get('jobUpdatedDate') or None
        job.language=data.get('locale')
        job.apply_url=urljoin(self.base,'apply?'+urlencode({'jobSeqNo':data['jobSeqNo']}))
        return job

    async def run(self):
        entry=await self.http.get(self.company.careers_url)
        soup=BeautifulSoup(entry.text,'lxml')
        links={urljoin(str(entry.url),a['href']) for a in soup.select('a[href]')
               if urlsplit(urljoin(str(entry.url),a['href'])).hostname=='mycareer.airasia.com'
               and urlsplit(a['href']).path.endswith('/search-results')}
        if len(links)!=1:
            raise ValueError('AirAsia official Search Jobs destination is missing or ambiguous')
        self.search_url=links.pop()
        async with HTTP(None,delay=self.http.delay,timeout=self.http.client.timeout,retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            search=await api.get(self.search_url)
            self.config=embedded_page_object(search.text,'var phApp = phApp || ')
            self.endpoint=self.config.get('widgetApiEndpoint')
            self.base=self.config.get('baseUrl','')
            if (self.config.get('refNum')!='AIRAGLOBAL' or self.config.get('siteType')!='external'
                    or self.endpoint!='https://mycareer.airasia.com/widgets' or self.base!='https://mycareer.airasia.com/gb/en/'):
                raise ValueError('AirAsia native global career configuration changed')
            self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,official_entry=str(entry.url),
                                        company_search_link=self.search_url,
                                        listing_authority='AirAsia official Search All Jobs portal, all its published brands and locations')
            ids=set()
            start=0
            while True:
                rows,total=await self.listing(api,start)
                if self.result.expected_total is None:
                    self.result.expected_total=total
                elif total!=self.result.expected_total:
                    self.result.errors.append('airasia_listing_total_changed')
                if not rows and len(ids)<self.result.expected_total:
                    self.result.errors.append('airasia_listing_ended_early')
                    break
                for row in rows:
                    identity=row.get('jobId')
                    if not identity or not row.get('jobSeqNo') or identity in ids:
                        raise ValueError('AirAsia native posting identity missing or repeated across pages')
                    ids.add(identity)
                    self.add(self.job({'listing':row},title=row['title'],job_id=identity,
                                      url=urljoin(self.base,'job/'+identity),date_posted=row.get('postedDate')))
                    if self.limit():
                        break
                if 'sample_limit' in self.result.warnings or len(ids)>=self.result.expected_total:
                    break
                start+=len(rows)
            self.result.listing_complete=(len(ids)==self.result.expected_total and not self.result.errors
                                          and 'sample_limit' not in self.result.warnings)
            self.result.jobs=await self.hydrate(self.result.jobs,lambda job:self.detail(api,job))
            if 'sample_limit' not in self.result.warnings:
                final=set()
                start=0
                while len(final)<self.result.expected_total:
                    rows,total=await self.listing(api,start)
                    current={row['jobId'] for row in rows}
                    if not current or final & current or total!=self.result.expected_total:
                        self.result.errors.append('airasia_final_listing_did_not_reconcile')
                        break
                    final.update(current)
                    start+=len(rows)
                if final!=ids:
                    self.result.listing_complete=False
                    self.result.errors.append('airasia_listing_id_set_changed')
            self.result.metadata['company_published_job_ids']=sorted(ids)
        return self.result
