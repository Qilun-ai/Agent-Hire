"""Amagi's own open-roles embed and its exact public MyNextHire detail requests."""
from base64 import b64encode
import gzip
from hashlib import sha256
import json
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import HTTP
from ..models import now


JOB_FIELDS = frozenset('reqId reqTitle expMin expMax location locationAddress jdDisplay approvedOn employmentType buName buDescription buURL totalPositions positionType mandatorySkillList nonMandatorySkillList educationList locationList roleName designation careerStream jobLevel fresher countryCode complianceNotices statusId requisitionPublished'.split())


def native_link(base, identity, page_type='jd'):
    context = {'pageType':page_type,'cvSource':'careers','reqId':int(identity),
               'requester':{'id':'','code':'','name':''},'page':'careers','bufilter':-1,'customFields':{}}
    encoded = b64encode(json.dumps(context,separators=(',',':')).encode()).decode()
    return base+'?'+quote('src=careers&p='+encoded,safe='')


class Amagi(Adapter):
    VERIFICATION_REVISION = 'amagi.official-open-roles-embed-exact-v2-details.1'
    BOARD = 'https://amagi.mynexthire.com'
    DETAIL = 'https://amagi.prod.us1.mynexthire.io/d17/careers/requisition/object'

    def evidence(self, url, data, body=None):
        if not self.http.output:
            return
        payload = json.dumps(data,ensure_ascii=False).encode()
        digest = sha256(payload).hexdigest()
        target = self.http.output/'responses'/f'{digest}.gz'
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(payload))
        with (self.http.output/'requests.jsonl').open('a') as handle:
            handle.write(json.dumps({'url':url,'final_url':url,'method':'POST' if body else 'GET',
                                     'request_body':body,'status':200,'sha256':digest,'fetched_at':now(),
                                     'evidence_scope':'public job fields only; unrelated coordinator/manager/personnel and applicant fields omitted'})+'\n')

    async def listing(self, api):
        if not self.page_guard():
            raise ValueError('Amagi public listing request budget exhausted')
        url = self.BOARD+'/employer/careers/reqlist/get'
        body = {'source':'careers','code':'','filterByBuId':-1}
        response = await api.json(url,method='POST',json=body)
        rows = response.get('reqDetailsBOList') if isinstance(response,dict) else None
        if not isinstance(rows,list):
            raise ValueError('Amagi embedded public job listing schema changed')
        ids = [row.get('reqId') for row in rows]
        if any(not isinstance(i,int) or i < 0 for i in ids) or len(set(ids)) != len(rows):
            raise ValueError('Amagi public listing contains missing or repeated requisition IDs')
        if any(not row.get('reqTitle') for row in rows):
            raise ValueError('Amagi public listing contains an unidentified posting')
        clean = [{key:value for key,value in row.items() if key in JOB_FIELDS} for row in rows]
        self.evidence(url,{'reqDetailsBOList':clean},body)
        return clean

    async def detail(self, api, job):
        identity = int(job.job_id)
        if identity not in self.published_ids:
            raise ValueError('Amagi detail ID was not exposed by its official embedded listing')
        body = {'source':'careers','id':'','code':'','reqId':identity}
        value = await api.json(self.DETAIL,method='POST',json=body)
        row = value.get('data',{}).get('reqDetailsBO') if isinstance(value,dict) else None
        if (not isinstance(row,dict) or value.get('status') != 200 or row.get('reqId') != identity
                or row.get('reqTitle') != job.title or row.get('requisitionPublished') is not True):
            raise ValueError('Amagi exact public detail identity/title/publication does not match its listed posting')
        row = {key:item for key,item in row.items() if key in JOB_FIELDS}
        if not isinstance(row.get('jdDisplay'),str) or not row['jdDisplay'].strip():
            raise ValueError('Amagi full native rendered job description field is missing')
        self.evidence(self.DETAIL,{'status':200,'data':{'reqDetailsBO':row}},body)
        job.raw['detail'] = row
        job.description_html = row['jdDisplay']
        job.description_method = 'amagi.official_embed.exact_v2_jdDisplay'
        job.description_source_url = self.DETAIL
        job.locations = row.get('locationList') or job.locations
        job.experience = {'minimum_years':row.get('expMin'),'maximum_years':row.get('expMax')}
        job.education = row.get('educationList')
        job.raw['native_detail_url'] = job.url
        return job

    async def run(self):
        entry = await self.http.get(self.company.careers_url)
        soup = BeautifulSoup(entry.text,'lxml')
        if self.company.name == 'Amagi Media Labs':
            policy_links = {urljoin(str(entry.url),a['href']) for a in soup.select('a[href]')
                            if urlsplit(urljoin(str(entry.url),a['href'])).hostname == 'www.amagi.com'
                            and urlsplit(a['href']).path.rstrip('/') == '/website-privacy-policy'}
            if len(policy_links) != 1:
                raise ValueError('Amagi Media Labs official publisher identity link is missing or ambiguous')
            # The published footer's trailing-slash link returns 404 over HTTP;
            # the inspected browser journey resolves to this canonical no-slash
            # policy. Normalize only that verified company-specific destination.
            published_policy = policy_links.pop()
            policy = await self.http.get(published_policy.rstrip('/'))
            publisher = BeautifulSoup(policy.text,'lxml').get_text(' ',strip=True)
            if (urlsplit(str(policy.url)).hostname != 'www.amagi.com'
                    or not re.search(r'Amagi\s+Media\s+Labs\s+(?:Private\s+)?Limited.{0,240}collectively.{0,35}Amagi',publisher,re.I)):
                raise ValueError('Amagi Media Labs legal publisher is not confirmed as the Amagi brand')
            self.result.metadata.update(requested_name_alias_of='Amagi',legal_publisher_identity_verified=True,
                                        identity_source_url=str(policy.url),
                                        published_identity_link=published_policy,
                                        alias_snapshot_provenance=self.options.previous_report.get('metadata',{}).get('alias_snapshot_provenance'))
        links = {urljoin(str(entry.url),a['href']) for a in soup.select('a[href]')
                 if urlsplit(urljoin(str(entry.url),a['href'])).hostname == 'www.amagi.com'
                 and urlsplit(a['href']).path.rstrip('/') == '/careers/open-roles'}
        if len(links) != 1:
            raise ValueError('Amagi official careers page has no unambiguous Open Roles destination')
        listing_url = links.pop()
        page = await self.http.get(listing_url)
        embed = BeautifulSoup(page.text,'lxml')
        scripts = {urljoin(str(page.url),n['src']) for n in embed.select('script[src]')}
        integration = self.BOARD+'/employer/ui/js/jobboard/careers-integration.js'
        if (integration not in scripts or not embed.select_one('iframe#mnhembedded')
                or not re.search(r'mnh_ci_onreadystatechange\(\s*[\'"]careers[\'"]\s*,\s*[\'"]amagi[\'"]\s*\)',page.text)):
            raise ValueError('Amagi official page no longer embeds the inspected public company tenant')
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION,
                                    official_entry=str(entry.url),official_open_roles_url=listing_url,
                                    embedded_integration_script=integration,
                                    listing_authority='Only the Amagi public careers request embedded by its own Open Roles page',
                                    date_semantics='approvedOn is approval/sort metadata, not a posting timestamp; unpublished posting dates remain null')
        async with HTTP(None,delay=self.http.delay,timeout=self.http.client.timeout,retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            config_url = self.BOARD+'/employer/jobboard/details_by_shortname/get/amagi/'
            config = await api.json(config_url)
            career_config = config.get('tenantPreferencesMap',{}).get('career_page_url',{})
            urls = {value.get('pageType'):value.get('url') for value in career_config.get('url',[])}
            if (config.get('clientName') != 'Amagi' or urlsplit(config.get('companyWebsite','')).hostname != 'www.amagi.com'
                    or urls.get('list') != listing_url or urls.get('jd') != 'https://www.amagi.com/job-description'
                    or urls.get('application') != urls.get('jd')):
                raise ValueError('Amagi public tenant branding/native URL configuration changed')
            self.evidence(config_url,{'clientName':config['clientName'],'companyWebsite':config['companyWebsite'],
                                      'career_page_url':{'url':career_config['url'],'requisitions_list_columns':career_config.get('requisitions_list_columns')}})
            rows = await self.listing(api)
            self.published_ids = {row['reqId'] for row in rows}
            self.result.expected_total = len(rows)
            for row in rows:
                identity = row['reqId']
                self.add(self.job({'listing':row},title=row['reqTitle'],job_id=identity,
                                  url=native_link(urls['jd'],identity),apply_url=native_link(urls['application'],identity,'application'),
                                  employer='Amagi',locations=row.get('locationList') or [row.get('location')],
                                  employment_type=row.get('employmentType'),department=row.get('buName'),
                                  experience={'minimum_years':row.get('expMin'),'maximum_years':row.get('expMax')},
                                  education=row.get('educationList')))
                if self.limit():
                    break
            old_cache = self.options.cached_jobs
            listing_by_id = {str(r['reqId']):r for r in rows}
            self.options.cached_jobs = {url:j for url,j in old_cache.items()
                                        if j.description_method == 'amagi.official_embed.exact_v2_jdDisplay'
                                        and j.job_id in listing_by_id and j.title == listing_by_id[j.job_id]['reqTitle']
                                        and j.raw.get('detail',{}).get('reqId') == int(j.job_id)}
            try:
                self.result.jobs = await self.hydrate(self.result.jobs,lambda job:self.detail(api,job))
            finally:
                self.options.cached_jobs = old_cache
            if 'sample_limit' not in self.result.warnings:
                final = await self.listing(api)
                if {row['reqId'] for row in final} != self.published_ids:
                    self.result.errors.append('amagi_official_embed_membership_changed')
            self.result.metadata['company_published_job_ids'] = sorted(self.published_ids)
            self.result.metadata['ui_pagination'] = 'Client-side 10-row pages over the entire returned public list; independently observed 10+4 jobs'
            self.result.listing_complete = not self.result.errors and 'sample_limit' not in self.result.warnings
        return self.result
