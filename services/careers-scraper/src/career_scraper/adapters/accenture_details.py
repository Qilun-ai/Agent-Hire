"""Exact-job fallback for Accenture's native postings, never ATS enumeration."""
from hashlib import sha256
from html import escape
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import now, text_html
from .workday import coordinates


def title_identity(value):
    return re.sub(r"\s+", " ", value).strip().casefold()


def placeholder(value):
    lines = [line.strip().casefold().rstrip('.') for line in value.splitlines() if line.strip().strip('.')]
    allowed = {'n/a', 'na', 'not applicable', 'not available', 'tbd', 'tbc', 'by email',
               'shared w recruitment', 'shared with recruitment', 'as per jd', 'refer jd', 'jd attached'}
    return not lines or all(line in allowed for line in lines)


def workday_role_prefix(html):
    """The inspected Workday footer cannot turn N/A into a verified role."""
    text = text_html(html)
    marker = re.search(r"\babout\s+accenture\b", text, re.I)
    if not marker:
        return text, not text or placeholder(text)
    core, footer = text[:marker.start()].strip(), text[marker.start():]
    lower = footer.casefold()
    generic = (('accenture is a leading global professional services company' in lower
                or 'accenture is a global professional services company' in lower)
               and 'equal employment opportunity statement' in lower)
    # A role after an introductory About section requires review; it is not a missing role.
    if re.search(r"\b(?:responsibilities|qualifications|requirements|what you will do|your role)\b", footer, re.I):
        generic = False
    return core, generic


async def recover_exact(http, job):
    native = urlsplit(job.url)
    if native.hostname != 'www.accenture.com' or parse_qs(native.query).get('id') != [job.job_id]:
        raise ValueError('Accenture recovery requires its exact company-discovered job URL')
    response = await http.get(job.url)
    final = urlsplit(str(response.url))
    if final.hostname != native.hostname or parse_qs(final.query).get('id') != [job.job_id]:
        raise ValueError('Native recovery page redirected away from the posting ID')
    soup = BeautifulSoup(response.text, 'lxml')
    title = soup.select_one('h1.rad-job-details-hero__title')
    if title is None or title_identity(title.get_text(' ', strip=True)) != title_identity(job.title):
        raise ValueError('Native recovery title does not match the discovered job')
    sections = []
    core = []
    for element in soup.select('.rad-job-detail__accordion > .rad-accordion-atom'):
        content = element.select_one('.rad-accordion-atom__content')
        heading = element.select_one('h2')
        if content is not None:
            sections.append((heading.get_text(' ', strip=True) if heading else '', content.decode_contents()))
            if element.get('id', '').endswith(('-job-description', '-qualification')):
                core.append(content.decode_contents())
    native_core = text_html('\n'.join(core))
    if not core:
        raise ValueError('Native job role containers are missing; absence cannot certify an unavailable description')
    proof = {'checked_at':now(), 'native_url':job.url, 'native_guid':job.job_id, 'native_title':title.get_text(' ', strip=True),
             'native_response_sha256':sha256(response.content).hexdigest(), 'native_role_text':native_core}
    job.raw['exact_job_description_check'] = proof
    if len(native_core.strip()) >= 100:
        html = '\n'.join(f'<section><h2>{escape(name)}</h2>{body}</section>' for name, body in sections)
        job.raw['native_page_recovery_html'] = html
        job.description_html, job.description_text = html, ''
        job.description_method = 'accenture.native_job_page.full_accordion_sections'
        job.description_source_url = job.url
        proof['status'] = 'recovered_native_page'
    else:
        req = job.requisition_id
        links = []
        gateways = []
        for a in soup.select('a[href]'):
            href = urljoin(str(response.url), a['href'])
            parsed = urlsplit(href)
            if (req and not parsed.username and not parsed.password
                    and parsed.hostname and parsed.hostname.startswith('accenture.')
                    and parsed.hostname.endswith('.myworkdayjobs.com') and '/job/' in parsed.path
                    and re.search(r'_' + re.escape(req) + r'(?:-\d+)?(?:/|$)', parsed.path)):
                links.append(href)
            if parsed.hostname == 'mycareer.accenture.com' and parse_qs(parsed.query).get('JRID') == [req]:
                gateways.append(href)
        if not links:
            if gateways and placeholder(native_core):
                # This exact JRID route was navigated and redirects to candidate
                # authentication. It exposes no additional public role details.
                job.apply_url = gateways[0]
                job.description_unavailable = True
                proof.update(status='publisher_placeholder_with_authenticated_application_gateway',
                             native_linked_application_gateway=gateways[0],
                             gateway_requirement_reference='companies/accenture/manual-reference.json')
                if 'source_role_description_unavailable' not in job.warnings:
                    job.warnings.append('source_role_description_unavailable')
                job.validate()
            else:
                proof['status'] = 'no_linked_exact_job_detail_available'
            return job
        apply_url = next((url for url in links if urlsplit(url).path.endswith('/apply')), links[0])
        parsed = urlsplit(apply_url)
        origin, tenant, site = coordinates(apply_url)
        detail_path = parsed.path.split('/' + site, 1)[1].removesuffix('/apply')
        api = f'{origin}/wday/cxs/{tenant}/{site}{detail_path}'
        job.apply_url = gateways[0] if gateways else apply_url
        proof.update(native_linked_apply_url=apply_url, exact_ats_api_url=api)
        data = await http.json(api)
        info = data.get('jobPostingInfo', {})
        if info.get('jobReqId') != req or title_identity(info.get('title', '')) != title_identity(job.title):
            raise ValueError('Linked Workday response does not match the native requisition/title')
        job.raw['linked_workday_detail'] = info
        role, known_footer = workday_role_prefix(info.get('jobDescription') or '')
        proof.update(ats_requisition_id=info['jobReqId'], ats_role_text=role)
        if len(role.strip()) >= 100:
            original = job.description_html
            body = info['jobDescription']
            job.raw['native_description_before_recovery'] = original
            job.description_html = f'<section><h2>Job Description</h2>{body}</section>\n{original}'
            job.description_text = ''
            job.description_method = 'accenture.linked_workday.exact_job_description'
            job.description_source_url = api
            proof['status'] = 'recovered_linked_exact_job'
        elif known_footer and placeholder(native_core) and placeholder(role):
            job.description_unavailable = True
            if 'source_role_description_unavailable' not in job.warnings:
                job.warnings.append('source_role_description_unavailable')
            proof['status'] = 'publisher_placeholder_in_native_and_linked_job'
        else:
            proof['status'] = 'short_role_requires_manual_review'
    if proof['status'].startswith('recovered_'):
        job.description_unavailable = False
        job.warnings = [w for w in job.warnings if w not in ('description_not_full_field', 'source_role_description_unavailable', 'detail_fetch_failed')]
    job.validate()
    return job
