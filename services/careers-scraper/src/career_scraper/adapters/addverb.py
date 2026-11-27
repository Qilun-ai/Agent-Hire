"""Addverb's inspected current-openings journey, not its marketing role profiles."""
import asyncio
import gzip
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..http import HTTP
from ..models import now, text_html


JOB_FIELDS = frozenset('employeeType seniorityName functionName currencyName designation company businessUnit department subDepartment region branch subBranch currentStatus isClosedForAll canAddCandidate qualificationList preferredLocationList requestId jobCode jobTitle jobType noOfOpening jobDescriptionBody jobDescriptionBodyWithHtml documentFileName uploadedFileName skillsName experienceFrom experienceTo qualificationCode salaryFrom salaryTo currencyCode companyCode businessUnitCode regionCode branchCode subBranchCode departmentCode subDepartmentCode designationCode preferredLocations projectName preferredLocationOptionName lastApplicationDate applicantGuidelines applicantGuidelinesBodyWithHtml jobFieldOne jobFieldTwo jobFieldThree jobFieldFour jobFieldFive jobFieldSix jobFieldSeven jobFieldEight jobFieldNine jobFieldTen'.split())
FILTERS = 'departmentCode companyCode careerPortalType jobTitle employmentType seniorityName jobFunction company businessUnitCode department subDepartment gradeCode designationCode levelCode branchCode subBranchCode regionCode locationId experience qualification skillsName urgentOpening jobPosted nationality preferredLocationId'.split()
API = 'https://gateway.app.hrone.cloud/api/external/public'


class Addverb(Adapter):
    VERIFICATION_REVISION = 'addverb.official-hrone-list-and-documents.1'

    def evidence(self, url, data, *, method='GET', request_body=None, scope='public_job_fields'):
        # The detail API also returns unrelated internal staff objects. Those
        # are excluded before any raw job evidence or normalized data is saved.
        if not self.http.output:
            return
        content = json.dumps(data, ensure_ascii=False).encode()
        digest = sha256(content).hexdigest()
        target = self.http.output/'responses'/f'{digest}.gz'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(content))
        with (self.http.output/'requests.jsonl').open('a') as handle:
            handle.write(json.dumps({'url':url, 'final_url':url, 'method':method, 'request_body':request_body,
                                     'status':200, 'sha256':digest, 'evidence_scope':scope, 'fetched_at':now()})+'\n')

    async def listing(self, api, page):
        if not self.page_guard():
            raise ValueError('Addverb listing request budget exhausted')
        body = {key:'' for key in FILTERS}
        body.update(companyCode=self.query['cc'][0], careerPortalType=self.query['rqt'][0], jobPosted='0',
                    isShortUrl=False, pagination={'pageNumber':page,'pageSize':15})
        url = API+'/referral/CareerPosition/Details'
        rows = await api.json(url, method='POST', json=body, headers=self.headers)
        if not isinstance(rows, list) or len(rows)>15:
            raise ValueError('Addverb public listing returned an error or ignored its page size')
        self.evidence(url, rows, method='POST', request_body=body)
        return rows

    def job_url(self, row):
        if row.get('jobApplicationPath'):
            result = row['jobApplicationPath']
        else:
            parsed = urlsplit(self.portal)
            extra = {'pid':row['encryptedPositionId'], 'dptc':row['departmentCode'], 'st':row['sourceType'],
                     'fm':'CR', 'headerColor':self.style.get('headerColor',''),
                     'fontSize':str(self.style.get('fontStyle','')), 'buttonColor':self.style.get('buttonColor','')}
            result = urlunsplit((parsed.scheme, parsed.netloc, '/apply-job',
                                 urlencode(parse_qsl(parsed.query)+list(extra.items())), ''))
        parsed = urlsplit(result)
        if parsed.hostname!='career.hrone.cloud' or parse_qs(parsed.query).get('pid')!=[row['encryptedPositionId']]:
            raise ValueError('Addverb listing supplied an unexpected exact-job destination')
        return result

    async def detail(self, api, job):
        row = job.raw['listing']
        url = f"{API}/referral/JobOpening/Request/Details/{row['encryptedPositionId']}/{self.query['cc'][0]}/{row['sourceType']}"
        data = await api.json(url, headers=self.headers)
        if (not isinstance(data, dict) or data.get('requestId')!=row['positionId']
                or data.get('jobCode')!=job.job_id or data.get('jobTitle')!=job.title or data.get('company')!='Addverb'):
            raise ValueError('Addverb detail identity/company does not match its advertised posting')
        data = {key:value for key,value in data.items() if key in JOB_FIELDS}
        self.evidence(url, data, scope='job_fields_only; unrelated staff/admin objects omitted')
        job.raw['detail'] = data
        body = data.get('jobDescriptionBodyWithHtml') or ''
        if not body:
            raise ValueError('Addverb full rich-text description field is missing')
        job.description_html = body
        job.description_text = text_html(body)
        job.description_method, job.description_source_url = 'addverb.hrone.full_body_and_published_attachments', url
        job.employment_type, job.workplace_type = data.get('employeeType'), data.get('preferredLocationOptionName')
        job.locations, job.department = data.get('preferredLocationList') or [], data.get('department')
        job.qualifications = data.get('qualificationList')
        job.experience = {'minimum':data.get('experienceFrom'), 'maximum':data.get('experienceTo'), 'unit':'years'}
        job.valid_through = data.get('lastApplicationDate')
        if data.get('applicantGuidelinesBodyWithHtml'):
            guide = data['applicantGuidelinesBodyWithHtml']
            job.description_html += '<section><h2>Applicant guidelines</h2>'+guide+'</section>'
            job.description_text += '\nApplicant guidelines\n'+text_html(guide)
        filename, uploaded = data.get('documentFileName'), data.get('uploadedFileName')
        if filename or uploaded:
            if not filename or not uploaded:
                raise ValueError('Addverb published attachment metadata is incomplete')
            file_api = API+'/access/FileVirtualUrl'
            info = await api.json(file_api, params={'fileDbName':uploaded,'requestSource':'null'}, headers=self.headers)
            file_url = info.get('fileVirtualPath') if isinstance(info, dict) else None
            if not file_url or urlsplit(file_url).hostname is None or not urlsplit(file_url).hostname.endswith('.blob.core.windows.net'):
                raise ValueError('Addverb attachment did not return the observed public document destination')
            response = await api.get(file_url)
            digest = sha256(response.content).hexdigest()
            if self.http.output:
                target = self.http.output/'attachments'/f'{job.job_id}-{digest[:16]}{Path(filename).suffix.lower()}'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
            started = time.perf_counter()
            extracted = await asyncio.to_thread(extract_attachment, response.content, filename)
            self.result.timings['attachment_extraction_seconds'] = self.result.timings.get('attachment_extraction_seconds',0.0)+(time.perf_counter()-started)
            job.raw['attachment'] = {'filename':filename, 'sha256':digest, 'source_url':file_api,
                                     'uploaded_file_name':uploaded, **extracted}
            job.description_html += '<section><h2>Attached job description</h2><pre>'+escape(extracted['text'])+'</pre></section>'
            job.description_text += '\nAttached job description\n'+extracted['text']
        return job

    async def run(self):
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url)
        link = BeautifulSoup(entry.text,'lxml').select_one('#career_section a[href]')
        if link is None:
            raise ValueError('Addverb current-openings link is missing; team role profiles are not vacancies')
        opening = urljoin(str(entry.url), link['href'])
        portal = await self.http.get(opening)
        self.portal = str(portal.url)
        self.query = parse_qs(urlsplit(self.portal).query)
        if urlsplit(self.portal).hostname!='career.hrone.cloud' or self.query.get('dc')!=['addverb'] or not all(self.query.get(k) for k in ('appId','rqt','cc')):
            raise ValueError('Official Addverb opening link did not reach its inspected career portal')
        self.headers = {'domainCode':self.query['dc'][0], 'apiKey':self.query['appId'][0], 'AccessMode':'W',
                        'X-Requested-With':'https://career.hrone.cloud'}
        self.result.metadata.update(verification_revision=self.VERIFICATION_REVISION, listing_authority='official_company_current_openings_link',
                                    company_entry=str(entry.url), current_openings_link=opening, portal_url=self.portal)
        self.result.timings['official_entry_seconds'] = time.perf_counter()-started
        # Anonymous public API client; evidence is explicitly limited to job data.
        async with HTTP(None, delay=self.http.delay, timeout=self.http.client.timeout,
                        retries=self.http.retries, max_response_bytes=self.http.max_response_bytes) as api:
            settings_url=f"{API}/referral/CareerPortal/{self.query['rqt'][0]}/{self.query['cc'][0]}"
            settings = await api.json(settings_url, headers=self.headers)
            profiles=settings.get('portalDetails',[]) if isinstance(settings,dict) else []
            if len(profiles)!=1 or profiles[0].get('companyName')!='Addverb' or profiles[0].get('companyCode')!='addverb':
                raise ValueError('Addverb public portal branding is missing or ambiguous')
            self.style=profiles[0]
            all_ids, first_ids = set(), None
            page=1
            while True:
                rows=await self.listing(api,page)
                if first_ids is None:
                    first_ids=[row['jobCode'] for row in rows]
                for row in rows:
                    code=row.get('jobCode')
                    if not code or not code.startswith('Addverb') or code in all_ids:
                        raise ValueError('Addverb job membership is missing, foreign, or repeats across pages')
                    all_ids.add(code)
                    self.add(self.job({'listing':row},title=row['jobTitle'],job_id=code,url=self.job_url(row),
                                      record_identity=sha256(f'{self.company.name}|addverb|{row["positionId"]}|{code}'.encode()).hexdigest()))
                    if self.limit():
                        break
                if 'sample_limit' in self.result.warnings or len(rows)<15:
                    break
                page+=1
            self.result.expected_total=len(all_ids)
            self.result.listing_complete='sample_limit' not in self.result.warnings
            self.result.jobs=await self.hydrate(self.result.jobs,lambda job:self.detail(api,job))
            final=await self.listing(api,1)
            if [row['jobCode'] for row in final]!=first_ids:
                self.result.listing_complete=False
                self.result.errors.append('addverb_listing_changed_during_collection')
            self.result.metadata['company_published_job_codes']=sorted(all_ids)
        return self.result
