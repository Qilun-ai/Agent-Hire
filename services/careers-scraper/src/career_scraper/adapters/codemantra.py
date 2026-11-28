"""Codemantra's inspected, currently absent company careers journey.

The requested official entry is a Squarespace 404 page; the current site's
navigation and published sitemap expose no careers/jobs route. The only
careers alias in evidence is a historical WordPress-era Naukri-hosted
subdomain that the current published journey no longer links; it is not
membership authority and is never fetched as a listing below. A different
company (codemantrasolutions.com, Mohali) also has a /career/ page but is not
the requested Codemantra. This adapter deliberately reports unknown
membership and requires a fresh individual review if a company-published
careers listing ever appears.
"""
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter


ENTRY = 'https://codemantra.com/careers/'
HOME = 'https://www.codemantra.com/'
ABOUT = 'https://www.codemantra.com/about-us'
SITEMAP = 'https://www.codemantra.com/sitemap.xml'
PROFILE = 'https://www.linkedin.com/company/codemantraonline/'

# Any linked or sitemapped path naming a careers/jobs surface. Codemantra's
# current platform (Squarespace) would serve such a page from an official
# path, so a match would mean the published journey has changed.
CAREERS_PATH = re.compile(
    r'/(careers?|jobs?|hiring|join|vacanc|open[-_]roles|openings?)(/|$)', re.I)


def _is_careers_path(url):
    try:
        return bool(CAREERS_PATH.search(urlsplit(url).path))
    except ValueError:
        return False


def _careers_hrefs(soup, base):
    """Return linked careers/jobs hrefs after resolving relative paths."""
    from urllib.parse import urljoin
    return sorted({
        urljoin(base, a.get('href', ''))
        for a in soup.select('a[href]')
        if _is_careers_path(urljoin(base, a.get('href', '')))
    })


def _has_profile_link(soup):
    target = urlsplit(PROFILE)
    return any(
        urlsplit(a.get('href', '')).hostname == target.hostname
        and urlsplit(a.get('href', '')).path.rstrip('/') == target.path.rstrip('/')
        for a in soup.select('a[href]')
    )


class Codemantra(Adapter):
    """Verify the published no-careers journey; never enumerate another board."""

    VERIFICATION_REVISION = 'codemantra.official-journey-no-careers-published.1'

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
        self.result.errors.append('codemantra_official_journey_changed_requires_individual_membership_review')
        return self.result

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            company_membership_verified=False,
            ats_board_requests=0,
            exact_job_ids_exposed=[],
            journey=[],
            review_evidence='companies/codemantra/manual-reference.json',
            historical_alias_not_fetched=(
                'careers.codemantra.com CNAME to careersite.secure.naukri.com '
                '(2024 WordPress-era Naukri board) is not linked from the current '
                'Squarespace journey and is never used as current membership.'),
            distinct_company_not_fetched=(
                'codemantrasolutions.com (CodeMantra Solutions, Mohali) is a different '
                'company from the requested Codemantra and is never fetched.'),
        )
        self.result.expected_total = None
        self.result.listing_complete = False
        entry = await self._fetch(ENTRY)
        if entry is not None:
            return self._changed()
        if (not self.result.metadata['journey']
                or not self.result.metadata['journey'][-1].get('error', '').startswith('HTTP 404:')):
            self.result.errors.append('codemantra_native_careers_entry_unavailable_before_membership_review')
            return self.result

        home = await self._fetch(HOME)
        if home is None:
            return self._changed()
        home_soup = BeautifulSoup(home.text, 'lxml')
        if not _has_profile_link(home_soup) or _careers_hrefs(home_soup, HOME):
            return self._changed()
        self.result.metadata['official_social_identity_verified'] = True

        about = await self._fetch(ABOUT)
        if about is None:
            return self._changed()
        about_soup = BeautifulSoup(about.text, 'lxml')
        if _careers_hrefs(about_soup, ABOUT):
            return self._changed()

        sitemap = await self._fetch(SITEMAP)
        if sitemap is None:
            return self._changed()
        try:
            root = ElementTree.fromstring(sitemap.text.encode('utf-8'))
        except ElementTree.ParseError:
            return self._changed()
        namespace = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        published = [node.text or '' for node in root.findall('.//s:loc', namespace)]
        if not published or any(_is_careers_path(url) for url in published):
            return self._changed()

        self.result.metadata['site_scan'] = {
            'homepage_title': home_soup.title.text.strip() if home_soup.title else None,
            'homepage_careers_links': [],
            'about_careers_links': [],
            'about_title': about_soup.title.text.strip() if about_soup.title else None,
            'published_sitemap_urls': published,
            'sitemap_careers_urls': [],
        }
        self.result.errors.append(
            'codemantra_native_careers_404_and_no_company_published_careers_listing')
        return self.result
