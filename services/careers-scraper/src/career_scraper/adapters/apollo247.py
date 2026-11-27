"""Apollo247's inspected, currently unavailable company careers journey.

The requested native entry is 404. Its official social account published an
Apollo247-only careers hostname, which currently fails DNS. A hospital-group
board does not establish Apollo247 membership. This adapter deliberately reports
unknown membership and requires a fresh review if either published route changes.
"""
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter


ENTRY = 'https://www.apollo247.com/careers'
HOME = 'https://www.apollo247.com/'
PROFILE = 'https://www.linkedin.com/company/apollo247/'
POST = ('https://www.linkedin.com/posts/'
        'apollo247_careers-apollo247-activity-7000706264276058112-XqbI')
SHORTLINK = 'https://lnkd.in/dJtt4uRu'
PUBLISHED_SOURCE = 'https://careers.apollo247.com/jobs'


def _has_link(soup, expected):
    target = urlsplit(expected)
    return any(urlsplit(a['href']).hostname == target.hostname
               and urlsplit(a['href']).path.rstrip('/') == target.path.rstrip('/')
               for a in soup.select('a[href]'))


def _post_is_company_authored(soup):
    return any(urlsplit(a['href']).hostname in {'www.linkedin.com', 'in.linkedin.com'}
               and urlsplit(a['href']).path.rstrip('/') == '/company/apollo247'
               and a.get_text(' ', strip=True) == 'Apollo 24|7'
               and 'feed-actor-name' in a['href']
               for a in soup.select('a[href]'))


class Apollo247(Adapter):
    VERIFICATION_REVISION = 'apollo247.published-source-unavailable.1'

    async def _fetch(self, url):
        if not self.page_guard():
            return None
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            self.result.metadata['journey'].append(
                {'url': url, 'error': str(exc), 'outcome': 'request_failed'})
            return None
        self.result.metadata['journey'].append({
            'url': url, 'status': response.status_code, 'final_url': str(response.url),
            'redirects': [str(r.url) for r in response.history], 'outcome': 'received'})
        return response

    def _changed(self):
        self.result.errors.append('apollo247_official_journey_changed_requires_individual_membership_review')
        return self.result

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY, published_source=PUBLISHED_SOURCE,
            company_membership_verified=False, ats_board_requests=0,
            exact_job_ids_exposed=[], journey=[],
            review_evidence='companies/apollo247/manual-reference.json',
            recovery_publication_age='Company-authored LinkedIn post displays 3y; not a job posting date.',
        )
        self.result.expected_total = None
        self.result.listing_complete = False
        entry = await self._fetch(ENTRY)
        if entry is not None:
            return self._changed()
        if not self.result.metadata['journey'] or not self.result.metadata['journey'][-1].get('error', '').startswith('HTTP 404:'):
            self.result.errors.append('apollo247_native_entry_unavailable_before_membership_review')
            return self.result

        home = await self._fetch(HOME)
        if home is None or not _has_link(BeautifulSoup(home.text, 'lxml'), PROFILE):
            return self._changed()
        self.result.metadata['official_social_identity_verified'] = True

        # This exact historical company-authored post was found in public search
        # after following the own-site social identity. Never enumerate an ATS.
        post = await self._fetch(POST)
        if post is None:
            self.result.errors.append('apollo247_company_published_recovery_post_unavailable')
            return self.result
        soup = BeautifulSoup(post.text, 'lxml')
        if not _post_is_company_authored(soup) or not _has_link(soup, SHORTLINK):
            return self._changed()
        self.result.metadata['company_authored_recovery_publication_verified'] = True

        shortlink = await self._fetch(SHORTLINK)
        if shortlink is None:
            self.result.errors.append('apollo247_company_published_shortlink_unavailable')
            return self.result
        # The public shortlink currently returns an external-link disclosure.
        # If it redirects elsewhere or publishes another route, review afresh.
        if str(shortlink.url) != SHORTLINK or not _has_link(BeautifulSoup(shortlink.text, 'lxml'), PUBLISHED_SOURCE):
            return self._changed()
        source = await self._fetch(PUBLISHED_SOURCE)
        if source is not None:
            return self._changed()
        self.result.errors.append('apollo247_native_careers_404_and_company_published_careers_source_unavailable')
        return self.result
