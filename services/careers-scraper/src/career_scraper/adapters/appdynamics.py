"""Round-3 parent-board attribution flow for AppDynamics (user scope decision 2026-11-08).

AppDynamics was absorbed into Cisco parentage; the official AppDynamics careers
entry redirects to Cisco's Splunk landing. Membership (per the user decision),
in order of preference:

  Rule A (structured attribution): a parent-board posting whose structured
  field -- legal employer (`company`), `companyName`, `CiscoBrands`, `department`
  or `SupeOrg_Level1..8` -- explicitly names AppDynamics, or
  Rule B (fallback, where a posting exposes no such field): an explicit
  "AppDynamics" in the job title, or in the role's own description header
  naming AppDynamics as the hiring business.

Mentions of the product as a skill, technology list item or product capability
are NOT membership. Candidates are surfaced with the parent board's own keyword
search (`?keywords=AppDynamics`, the official journey's search surface), plus
an independent title census from the Cisco careers sitemap (title-derived
job slugs). Every candidate is fetched in full and classified.

The board exposes no business-unit/legal-employer facet, and org fields are
visible only per detail (keyword search does not index them), so a zero count
is never certified: this adapter always returns expected_total=None,
listing_complete=False and a precise residual error unless a journey change
requires individual review.
"""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

HEADER_CHARS = 1200
CONTEXT_CHARS = 160
# Cues that mark an "AppDynamics" mention as a product/skill reference rather
# than the hiring business (product lists, capability areas, requirements).
CONTEXT_CUES = (
    'experience', 'expertise', 'hands-on', 'such as', 'including', 'capabilities',
    'delivers', 'delivering', 'product', 'portfolio', 'offering', 'implementing',
    'managing', 'positioning', 'platform', 'technology', 'technologies',
)
HIRE_CUES = (
    'join us', 'join the', 'welcome to', 'hiring', 'we are hiring', 'careers at',
    'become part of', 'apply', 'about appdynamics', 'appdynamics is',
)


def _text(description_html):
    return re.sub(r'\s+', ' ', BeautifulSoup(description_html or '', 'lxml').get_text()).strip()


def _mentions(text):
    return [m.start() for m in re.finditer(r'appdynamics', text, re.I)]


def _hiring_business_header(text):
    """True when a header mention names AppDynamics as the hiring business.

    Conservative: a header mention only counts as hiring-business attribution
    when its context carries a hiring cue and no product/skill cue. Bare
    product-named mentions (e.g. "Splunk AppDynamics delivers ...") are
    product references, not business attribution.
    """
    header = text[:HEADER_CHARS]
    for start in _mentions(header):
        context = header[max(0, start - CONTEXT_CHARS):start + CONTEXT_CHARS].lower()
        has_product = any(cue in context for cue in CONTEXT_CUES)
        has_hire = any(cue in context for cue in HIRE_CUES)
        if has_hire and not has_product:
            return True
        if not has_product and not has_hire and re.search(
                r'(team|business|organization|organisation|company|division|unit)\s+(is\s+(?:recruiting|hiring)|of\s+appdynamics)', context):
            return True
    return False


class AppDynamics(Adapter):
    VERIFICATION_REVISION = 'appdynamics.parent-board-round3.1'
    LANDING = 'https://careers.cisco.com/global/en/splunk'
    KEYWORD_SEARCH = 'https://careers.cisco.com/global/en/search-results?keywords=AppDynamics'
    SITEMAPS = [f'https://careers.cisco.com/global/en/sitemap{n}.xml' for n in (1, 2, 3)]
    SEARCHES = {
        'Search jobs': ('/global/en/splunk/search-page', '(title.title_phenom:("Splunk"))'),
        'Explore open roles': ('/global/en/search-results', ''),
    }
    REGIONS = {
        '/global/en/splunk/poland': '(title.title_phenom:("Splunk") AND country.country_sort:("Poland"))',
        '/global/en/splunk/india': '(title.title_phenom:("Splunk") AND country.country_sort:("India"))',
        '/global/en/splunk/san-jose': '(title.title_phenom:("Splunk") AND city.city_sort:("San Jose"))',
    }
    FACETS = {'raasJobRequisitionType', 'country', 'RemoteType', 'city', 'state', 'type', 'category'}
    STRUCTURED_FIELDS = (
        'company', 'companyName', 'CiscoBrands', 'department',
        *(f'SupeOrg_Level{n}' for n in range(1, 9)),
    )
    CHANGED = 'appdynamics_official_journey_changed_requires_individual_membership_review'
    RESIDUAL = (
        'appdynamics_round3_no_parent_board_posting_meets_user_attribution_rule_'
        'but_board_exposes_no_business_unit_or_legal_employer_facet_and_org_fields_'
        'are_detail_only_unindexed_so_zero_is_not_certified'
    )

    # --- parsing helpers (pure; unit-tested on saved fixtures) ---
    @staticmethod
    def _keyword_snapshot(response):
        match = re.search(r'phApp\.ddo\s*=\s*', response.text)
        if not match:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(response.text[match.end():])
            search = data['eagerLoadRefineSearch']
            jobs = search['data']['jobs']
            return {
                'url': str(response.url), 'parent_total': search['totalHits'],
                'preview_count': len(jobs), 'query': search['eid']['query'],
                'facet_fields': [item['field'] for item in search['data']['aggregations']],
                'candidates': [
                    {'job_id': job['jobId'], 'title': job['title'],
                     'department': job.get('department'), 'req_id': job.get('reqId')}
                    for job in jobs
                ],
                'appdynamics_mentions': len(_mentions(response.text)),
            }
        except (ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _detail_job(response):
        match = re.search(r'phApp\.ddo\s*=\s*', response.text)
        if not match:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(response.text[match.end():])
            return data['jobDetail']['data']['job']
        except (ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _sitemap_census(response):
        locs = re.findall(r'<loc>([^<]+)</loc>', response.text)
        job_pattern = re.compile(
            r'/job/CISCISGLOBAL(\d+)EXTERNALENGLOBAL/(.+)$')
        jobs, title_hits = {}, []
        for loc in locs:
            match = job_pattern.search(loc)
            if match:
                jobs[match.group(1)] = match.group(2)
                if 'appdynamics' in match.group(2).lower():
                    title_hits.append(loc)
        return {'url': str(response.url), 'loc_count': len(locs),
                'job_locs': len(jobs), 'appdynamics_title_slugs': title_hits}, jobs

    @classmethod
    def classify(cls, *, title, detail):
        """Apply the round-3 membership rule to one candidate.

        Returns (include, rule, reason, evidence). A posting is included only
        when a structured field names AppDynamics (Rule A), the title names it
        (Rule B), or the description header names it as the hiring business
        (Rule B). Product/skill mentions in the body are exclusions.
        """
        structured = {f: detail.get(f) for f in cls.STRUCTURED_FIELDS if detail.get(f)}
        evidence = {'structured_fields': {k: v for k, v in structured.items()}}
        for field, value in structured.items():
            if 'appdynamics' in str(value).lower():
                return True, 'A_structured_field', f'structured field {field} names AppDynamics', evidence
        if 'appdynamics' in (title or '').lower():
            return True, 'B_title', 'job title names AppDynamics', evidence
        text = _text(detail.get('description', ''))
        evidence['description_mentions'] = len(_mentions(text))
        evidence['header'] = text[:HEADER_CHARS]
        if _hiring_business_header(text):
            return True, 'B_description_header', 'description header names AppDynamics as hiring business', evidence
        if evidence['description_mentions']:
            return False, 'excluded', 'AppDynamics appears only as product/skill mention, not as attributed hiring business', evidence
        return False, 'excluded', 'no AppDynamics attribution in structured fields, title, or description', evidence

    def _changed(self):
        self.result.errors.append(self.CHANGED)
        return self.result

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=self.company.careers_url,
            round3_user_scope_decision='2026-11-08',
            membership_rule=(
                'Rule A: structured field (legal employer, companyName, CiscoBrands, '
                'department, SupeOrg_Level1-8) names AppDynamics; else Rule B: '
                'AppDynamics in job title or description header as hiring business; '
                'product/skill mentions excluded.'),
            company_membership_verified=False, parent_jobs_collected=0,
            ats_board_requests=0, detail_requests=0,
            review_evidence='companies/appdynamics/journey-research.md',
            scope_observations=[], candidates=[], rule_counts={}, sitemap_census=[],
        )
        self.result.expected_total = None
        self.result.listing_complete = False
        try:
            if not self.page_guard():
                return self.result
            entry = await self.http.get(self.company.careers_url)
            self.result.metadata.update(
                redirect_chain=[{'url': str(r.url), 'status': r.status_code} for r in entry.history],
                final_url=str(entry.url),
            )
            if str(entry.url).rstrip('/') != self.LANDING:
                return self._changed()
            soup = BeautifulSoup(entry.text, 'lxml')
            if not soup.title or soup.title.get_text(strip=True) != 'Working at Splunk - Cisco Careers':
                return self._changed()
            if re.search('appdynamics', entry.text, re.I):
                return self._changed()
            links = [(a.get_text(' ', strip=True), urljoin(str(entry.url), a['href']))
                     for a in soup.select('a[href]')]
            for label, (path, _query) in self.SEARCHES.items():
                matches = [url for text, url in links if text == label
                           and urlsplit(url).hostname == 'careers.cisco.com'
                           and urlsplit(url).path == path and not urlsplit(url).query]
                if len(set(matches)) != 1:
                    return self._changed()
            for path in self.REGIONS:
                matches = [url for text, url in links if text == 'See job openings'
                           and urlsplit(url).hostname == 'careers.cisco.com'
                           and urlsplit(url).path == path and not urlsplit(url).query]
                if len(set(matches)) != 1:
                    return self._changed()
            # The board's own keyword search surfaces every posting whose
            # title/description text names AppDynamics (probe-verified:
            # indexed on description body text; org fields are NOT indexed).
            if not self.page_guard():
                return self.result
            search = await self.http.get(self.KEYWORD_SEARCH)
            snapshot = self._keyword_snapshot(search)
            if (str(search.url) != self.KEYWORD_SEARCH or not snapshot
                    or snapshot['query'] != 'appdynamics'
                    or set(snapshot['facet_fields']) != self.FACETS
                    or snapshot['parent_total'] != snapshot['preview_count']):
                return self._changed()
            self.result.metadata['scope_observations'].append(snapshot)
            candidate_ids = [c['job_id'] for c in snapshot['candidates']]
            # Independent title census from the careers sitemap (job slugs
            # are title-derived; sweep-verified over the full board).
            sitemap_jobs = {}
            for url in self.SITEMAPS:
                if not self.page_guard():
                    return self.result
                response = await self.http.get(url)
                if response.status_code != 200:
                    return self._changed()
                census, jobs = self._sitemap_census(response)
                self.result.metadata['sitemap_census'].append(census)
                sitemap_jobs.update(jobs)
            title_ids = sorted({re.search(r'CISCISGLOBAL(\d+)', loc).group(1)
                                  for census in self.result.metadata['sitemap_census']
                                  for loc in census['appdynamics_title_slugs']})
            for job_id in title_ids:
                if job_id not in candidate_ids:
                    candidate_ids.append(job_id)
            self.result.metadata['sitemap_total_job_locs'] = len(sitemap_jobs)
            # Fetch every candidate in full and classify under the rule.
            for job_id in candidate_ids:
                if not self.page_guard():
                    return self.result
                detail_url = f'https://careers.cisco.com/global/en/job/{job_id}'
                response = await self.http.get(detail_url)
                self.result.metadata['detail_requests'] += 1
                detail = self._detail_job(response)
                if detail is None or str(detail.get('jobId')) != str(job_id):
                    return self._changed()
                search_row = next((c for c in snapshot['candidates'] if c['job_id'] == job_id), None)
                include, rule, reason, evidence = self.classify(
                    title=detail.get('title') or (search_row or {}).get('title', ''), detail=detail)
                evidence.update({
                    'job_id': str(job_id), 'title': detail.get('title'),
                    'url': str(response.url), 'req_id': detail.get('reqId'),
                    'company_name': detail.get('companyName'), 'company': detail.get('company'),
                    'location': detail.get('location'), 'type': detail.get('type'),
                    'remote_type': detail.get('RemoteType'),
                    'posted_date': detail.get('jobPostingStartDate'),
                    'updated_date': detail.get('jobUpdatedDate'),
                    'apply_url': detail.get('applyUrl'),
                })
                self.result.metadata['rule_counts'][rule] = self.result.metadata['rule_counts'].get(rule, 0) + 1
                self.result.metadata['candidates'].append({
                    'include': include, 'rule': rule, 'reason': reason, **evidence})
                if include:
                    self.add(self.job(
                        detail, title=detail.get('title'), url=str(response.url),
                        job_id=str(detail.get('reqId') or job_id),
                        description=detail.get('description', ''), method='native_detail',
                        description_url=str(response.url),
                        location=detail.get('location'), job_type=detail.get('type'),
                        posted_date=detail.get('jobPostingStartDate')))
            if not self.result.jobs:
                self.result.errors.append(self.RESIDUAL)
            return self.result
        finally:
            self.result.timings['official_scope_probe_seconds'] = time.perf_counter() - started
