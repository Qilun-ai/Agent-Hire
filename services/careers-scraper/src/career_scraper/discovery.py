"""Discover job backends only through the company's official career website."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from html import unescape
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Company, Source, now
from .company_flows import COMPANY_ADAPTERS


def detect(url: str) -> Source | None:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.username or p.password or p.scheme not in {"https", "http"}:
        return None
    parts = p.path.strip("/").split("/")
    query = parse_qs(p.query)
    origin = f"{p.scheme}://{p.netloc}"
    board = None
    # These exact native entries have individually reviewed company flows. Keep
    # them selected after official-entry discovery instead of replacing them
    # with a generic provider merely because an application link uses an ATS.
    reviewed_entries = {
        ('www.cashfree.com', '/careers'):
            ('cashfree', 'https://careers.kula.ai/cashfree?jobs=true'),
        ('www.celigo.com', '/careers'):
            ('celigo', 'https://www.celigo.com/careers/'),
        ('www.bosch-softwaretechnologies.com', '/en/careers'):
            ('bosch_software', 'https://www.bosch-softwaretechnologies.com/en/careers/'),
        ('www.broadcom.com', '/company/careers'):
            ('broadcom', 'https://www.broadcom.com/company/careers'),
        ('www.browserstack.com', '/careers'):
            ('browserstack', 'https://browserstack.wd3.myworkdayjobs.com/External'),
        ('careers.calendly.com', ''):
            ('calendly', 'https://careers.calendly.com/'),
        ('calendly.com', '/careers'):
            ('calendly', 'https://careers.calendly.com/'),
        ('byjus.com', '/careers-at-byjus'):
            ('byjus', 'https://byjus.com/careers-at-byjus/'),
        ('jobs.bytedance.com', '/en/position'):
            ('bytedance', 'https://jobs.bytedance.com/en/position'),
        ('joinbytedance.com', '/search'):
            ('bytedance', 'https://jobs.bytedance.com/en/position'),
    }
    reviewed = reviewed_entries.get((host, p.path.rstrip('/')))
    if reviewed:
        return Source(*reviewed, evidence_url=url)
    if host == 'www.lifeatcanva.com' and p.path.rstrip('/') == '/en/jobs':
        return Source('canva','https://www.lifeatcanva.com/en/jobs/')
    if host == 'atlan.com' and p.path.rstrip('/') == '/careers':
        return Source('atlan','https://atlan.com/careers/')
    if host == 'www.arista.com' and p.path.rstrip('/') == '/en/careers':
        return Source('arista','https://www.arista.com/en/careers')
    if host == 'www.arcesium.com' and p.path.rstrip('/') == '/careers':
        return Source('arcesium','https://www.arcesium.com/careers')
    if host == 'careers.amd.com' and p.path.rstrip('/') == '/careers-home/jobs':
        return Source('amd','https://careers.amd.com/careers-home/jobs')
    if host == 'www.americanexpress.com' and p.path.rstrip('/') == '/en-us/careers':
        return Source('amex','https://www.americanexpress.com/en-us/careers/')
    if host == 'www.apollo247.com' and p.path.rstrip('/') == '/careers':
        return Source('apollo247','https://www.apollo247.com/careers')
    if host == 'www.appdynamics.com' and p.path.rstrip('/') == '/company/careers':
        return Source('appdynamics','https://www.appdynamics.com/company/careers')
    if host == 'www.amagi.com' and p.path.rstrip('/') == '/careers':
        return Source('amagi','https://www.amagi.com/careers')
    if host == 'awavesemi.com' and p.path.rstrip('/') == '/careers':
        return Source('alphawave','https://awavesemi.com/careers/')
    if host == 'www.airoha.com' and p.path.rstrip('/') == '/recruit/jobs':
        return Source('airoha','https://www.airoha.com/recruit/jobs')
    if host == 'careers.airasia.com' and p.path.rstrip('/')=='':
        return Source('airasia','https://careers.airasia.com/')
    if host == 'careers.adobe.com' and p.path.rstrip('/') in ('','/us/en'):
        return Source('adobe','https://careers.adobe.com/')
    if (host == "addverb.com" and p.path.rstrip('/') == '/careers') or (host == 'careers.addverb.com' and p.path.rstrip('/') in ('','/career')):
        return Source('addverb','https://addverb.com/careers/')
    if host == "www.accenture.com" and re.fullmatch(r"/[a-z]{2}-[a-z]{2}/careers/jobsearch/?", p.path):
        return Source("accenture", urlunsplit((p.scheme, p.netloc, p.path, "", "")))
    if host == "www.digitalocean.com" and p.path.rstrip("/") == "/careers":
        return Source("digitalocean", "https://www.digitalocean.com/careers")
    if host == "www.databricks.com" and p.path.rstrip("/") == "/company/careers/open-positions":
        return Source("databricks", "https://www.databricks.com/company/careers/open-positions")
    if host == "careers.datadoghq.com" and p.path.rstrip("/") == "/all-jobs":
        return Source("datadog", "https://careers.datadoghq.com/all-jobs/")
    if host == "www.lifeatspotify.com" and p.path.rstrip("/") == "/jobs":
        return Source("spotify", "https://www.lifeatspotify.com/jobs")
    if host == "www.42gears.com" and p.path.rstrip("/") == "/careers":
        return Source("42gears", "https://www.42gears.com/careers/")
    if host == "careers.airbnb.com" and p.path.startswith("/positions"):
        return Source("airbnb", "https://careers.airbnb.com/positions/")
    if host == "stripe.com" and p.path.startswith(("/jobs", "/careers/search", "/careers/listing")):
        return Source("stripe", "https://stripe.com/careers/search")
    if host == "www.google.com" and "/careers/applications/jobs/results" in p.path:
        return Source("google", "https://www.google.com/about/careers/applications/jobs/results/")
    if host == "jobs.apple.com" and re.match(r"/[a-z]{2}-[a-z]{2}/(?:search|details)", p.path):
        return Source("apple", "https://jobs.apple.com/en-us/search?location=")
    if host == "www.atlassian.com" and p.path.startswith("/company/careers"):
        return Source("atlassian", "https://www.atlassian.com/company/careers/all-jobs")
    if host == "careers.cred.club":
        return Source("cred", "https://careers.cred.club/")
    if host == "www.phonepe.com" and p.path.startswith("/careers/job-openings"):
        return Source("phonepe", "https://www.phonepe.com/careers/job-openings/")
    if host == "www.postman.com" and p.path.startswith("/company/careers/open-positions"):
        return Source("postman", "https://www.postman.com/company/careers/open-positions/")
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards-api.greenhouse.io", "api.greenhouse.io"}:
        if parts[:2] == ["v1", "boards"] and len(parts) >= 3:
            board = parts[2]
        elif parts[0] == "embed":
            board = query.get("for", [None])[0]
        elif parts[0] and host not in {"boards-api.greenhouse.io", "api.greenhouse.io"}:
            board = parts[0]
        if board and re.fullmatch(r"[a-zA-Z0-9_-]+", board) and board not in {"v1", "undefined", "null"}:
            return Source("greenhouse", f"https://job-boards.greenhouse.io/{board}")
    if host in {"jobs.lever.co", "jobs.eu.lever.co", "api.lever.co", "api.eu.lever.co"}:
        if host.startswith("api.") and not (parts[:2] == ["v0", "postings"] and len(parts) > 2):
            return None
        board = parts[2] if parts[:2] == ["v0", "postings"] and len(parts) > 2 else parts[0]
        if board and re.fullmatch(r"[a-zA-Z0-9_-]+", board) and board not in {"v0", "v1", "undefined", "null"}:
            region = ".eu" if ".eu.lever" in host else ""
            return Source("lever", f"https://jobs{region}.lever.co/{board}")
    if host in {"jobs.ashbyhq.com", "api.ashbyhq.com"}:
        board = parts[2] if parts[:2] == ["posting-api", "job-board"] and len(parts) > 2 else parts[0]
        if board and re.fullmatch(r"[a-zA-Z0-9_-]+", board) and board not in {"api", "undefined", "null"}:
            return Source("ashby", f"https://jobs.ashbyhq.com/{board}")
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com", "api.smartrecruiters.com"}:
        board = parts[2] if parts[:2] == ["v1", "companies"] and len(parts) > 2 else parts[0]
        if board:
            return Source("smartrecruiters", f"https://careers.smartrecruiters.com/{board}")
    if host.endswith(".myworkdayjobs.com"):
        from .adapters.workday import coordinates
        try:
            origin, _, site = coordinates(url)
            return Source("workday", f"{origin}/{site}")
        except ValueError:
            return None
    if host == "apply.workable.com" and parts[0] not in {"", "api", "j"}:
        return Source("workable", f"https://apply.workable.com/{parts[0]}/")
    if re.search(r"\.oraclecloud\d*\.com$", host) and "CandidateExperience" in parts:
        try:
            site = parts[parts.index("sites") + 1]
        except (ValueError, IndexError):
            return None
        return Source("oracle", f"{origin}/hcmUI/CandidateExperience/en/sites/{site}/jobs")
    if host.endswith(".eightfold.ai") and host.split(".")[0] not in {"www", "app", "trust", "vs-errors", "api", "docs", "support"}:
        if not (p.path.startswith("/careers") or p.path.startswith("/api/apply/")):
            return None
        return Source("eightfold", f"https://{p.netloc}/careers", {"domain": query["domain"][0]} if "domain" in query else {})
    if host.endswith(".recruitee.com"):
        return Source("recruitee", origin)
    if host == "jobs.gem.com" and parts[0] and parts[0] not in {"api", "assets"}:
        return Source("gem", origin + "/" + parts[0])
    if host.endswith(".keka.com") and p.path.startswith("/careers"):
        return Source("keka", origin + "/careers/")
    if host.endswith(".breezy.hr"):
        return Source("breezy", origin)
    if host.endswith(".jobs.personio.de") or host.endswith(".jobs.personio.com"):
        return Source("personio", origin)
    if host in {"amazon.jobs", "www.amazon.jobs"}:
        return Source("amazon", "https://www.amazon.jobs/en/search")
    return None


CAREER_LINK = re.compile(r"career|vacanc|opening|open.role|open.position|search.job|view.job|explore.job|all.job|join.us|join.(?:our|the).team|work.with.us|job.search|opportunit", re.I)
DETAIL_LINK = re.compile(r"/(?:jobs?|positions?|requisitions?|opportunities|job-detail|job-details)/[^/?#]+", re.I)
EXCLUDED = {"linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "glassdoor.com", "indeed.com"}


def links_from_html(html: str, base: str):
    soup = BeautifulSoup(html, "lxml")
    if (base_tag := soup.find("base", href=True)):
        base = urljoin(base, base_tag["href"])
    candidates = []
    for tag in soup.select("a[href], iframe[src], iframe[data-src], script[src], [data-url], [data-href]"):
        raw = tag.get("href") or tag.get("src") or tag.get("data-src") or tag.get("data-url") or tag.get("data-href")
        if raw:
            candidates.append((urljoin(base, unescape(raw)), tag.get_text(" ", strip=True), tag.name))
    # Script/config URLs are evidence only when they contain a complete, recognized board URL.
    decoded = unescape(html.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/"))
    for match in re.finditer(r'https?://[^\s<>"\x27`\\]+', decoded):
        url = match.group().rstrip(");,")
        host = urlsplit(url).hostname or ""
        base_host = urlsplit(base).hostname or ""
        domain = ".".join(base_host.split(".")[-2:])
        internal_career_url = (host == domain or host.endswith("." + domain)) and bool(CAREER_LINK.search(urlsplit(url).path)) and len(url) < 300
        if detect(url) or internal_career_url:
            candidates.append((url, "embedded board URL", "embedded_url"))
    return list(dict.fromkeys(candidates))


def detect_page(url, html):
    soup = BeautifulSoup(html, "lxml")
    if "app.jibecdn.com/" in html and "/prod/search/" in html:
        p = urlsplit(url)
        path = p.path if "/jobs" in p.path else p.path.rstrip("/") + "/jobs"
        return Source("jibe", f"{p.scheme}://{p.netloc}{path}",
                      {"detected_from_html": "app.jibecdn.com/prod/search"}, evidence_url=url)
    node = soup.find("code", id="pcsx-data")
    if node:
        try:
            config = json.loads(node.get_text())
            p = urlsplit(url)
            return Source("pcsx", f"{p.scheme}://{p.netloc}/careers",
                          {"domain": config["domain"], "detected_from_html": "code#pcsx-data"}, evidence_url=url)
        except (ValueError, KeyError):
            pass
    return None


def link_priority(url):
    path = urlsplit(url).path.lower().rstrip("/")
    if re.search(r"(?:/jobs|/search|/positions|/open-positions|/job-openings|/opportunities)$", path):
        return 0
    if re.search(r"life|benefit|culture|resource|blog|news|article|privacy|values|events", path):
        return 5
    return 2


@dataclass
class Discovery:
    company: str
    careers_url: str
    sources: list[Source] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=now)

    def to_dict(self):
        return asdict(self)


async def discover(http, company: Company, *, max_pages=16, browser=False) -> Discovery:
    result = Discovery(company.name, company.careers_url)
    if not company.enabled:
        result.warnings.append("skipped_by_user")
        return result
    if not company.careers_url:
        result.errors.append(company.notes or "missing_official_careers_url")
        return result
    queue = [(company.careers_url, 0)]
    seen, sources = set(), {}
    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            response = await http.get(url)
            final, html = str(response.url), response.text
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not any(t in content_type for t in ("html", "text/plain", "json")):
                result.warnings.append(f"skipped_non_career_document: {final}")
                continue
            result.pages.append(final)
            if final != url:
                result.edges.append({"from": url, "to": final, "kind": "http_redirect",
                                     "hops": [str(r.url) for r in response.history]})
            native_entry = detect(url)
            # Preserve the explicitly implemented company journey when its entry
            # redirects. A parent's generic board must not replace that flow.
            direct = (native_entry if native_entry and native_entry.adapter == COMPANY_ADAPTERS.get(company.name)
                      else detect_page(final, html) or detect(final))
            if direct:
                direct = company.resolve_source(direct)
                direct.evidence_url = url
                sources[direct.key] = direct
                continue
            candidates = links_from_html(html, final)
            if not any(detect(u) for u, _, _ in candidates):
                soup = BeautifulSoup(html, "lxml")
                scripts = [urljoin(final, node.get("src") or node.get("component-url"))
                           for node in soup.select("script[src], astro-island[component-url]")]
                # Inspect only career-related application scripts, never arbitrary vendor bundles.
                scripts = [u for u in scripts if re.search(r"career|job|position|opening", urlsplit(u).path, re.I)
                           and not re.search(r"vendor|polyfill|analytics", u, re.I)]
                scripts.sort(key=lambda u: (not bool(re.search(r"listing|jobs|search|opening", u, re.I)), len(u)))
                scripts = scripts[:8]
                for script_url in scripts:
                    try:
                        script = await http.get(script_url)
                        found_links = [(u, label, kind) for u, label, kind in links_from_html(script.text, script_url) if detect(u)]
                        if found_links:
                            result.edges.append({"from": final, "to": script_url, "kind": "script_reference"})
                            for u, label, kind in found_links:
                                result.edges.append({"from": script_url, "to": u, "kind": kind})
                            candidates += found_links
                    except Exception:
                        # Optional script inspection cannot hide a failed board scrape; source status is separate.
                        continue
            if browser and not any(detect(u) for u, _, _ in candidates):
                from .browser import render
                rendered = await render(final, http.output)
                if rendered["url"] != final:
                    result.edges.append({"from": final, "to": rendered["url"], "kind": "browser_redirect"})
                final = rendered["url"]
                direct = detect(final)
                if direct:
                    direct = company.resolve_source(direct)
                    direct.evidence_url = url
                    sources[direct.key] = direct
                candidates += links_from_html(rendered["html"], final)
                candidates += [(u, "browser network", "network") for u in rendered["requests"] if detect(u)]
            for target, label, kind in candidates:
                parsed = urlsplit(target)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                    continue
                found = detect(target)
                if found:
                    found = company.resolve_source(found)
                    if found.key not in sources:
                        found.evidence_url = final
                        sources[found.key] = found
                        result.edges.append({"from": final, "to": target, "kind": kind, "label": label[:180]})
                    continue
                if depth >= 2 or any(parsed.hostname == h or parsed.hostname.endswith("." + h) for h in EXCLUDED):
                    continue
                if not CAREER_LINK.search(label + " " + parsed.path):
                    continue
                if re.search(r"\.(pdf|png|jpe?g|gif|webp|svg|css|js|mp4|m4v|mov|webm|mp3|wav|zip|gz|woff2?|ttf)$", parsed.path, re.I):
                    continue
                target = urlunsplit(parsed._replace(fragment=""))
                if target not in seen and target not in [item[0] for item in queue]:
                    same_site = parsed.hostname == urlsplit(final).hostname
                    if not same_site and not re.search(r"^(?:view|explore|search|see|find|browse|apply|current|open|join|work)\b", label, re.I) and kind not in {"iframe", "embedded_url"}:
                        continue
                    queue.append((target, depth + 1))
                    result.edges.append({"from": final, "to": target, "kind": kind, "label": label[:180]})
            queue.sort(key=lambda item: (link_priority(item[0]), item[1]))
            if sources:
                # All links on the page were inspected. Do not expand culture, news, or advice pages
                # after a real job board is linked; only additional listing entry points matter.
                queue = [item for item in queue if link_priority(item[0]) == 0]
        except Exception as exc:
            native_entry = detect(url)
            if (depth == 0 and native_entry and native_entry.adapter == COMPANY_ADAPTERS.get(company.name)):
                # An inspected native flow may own recovery from a missing entry.
                # Let that exact company handler report/recover it, without a
                # generic homepage/ATS walk or a false discovery-level zero.
                native_entry.evidence_url = url
                native_entry.options['official_entry_fetch_error'] = f'{type(exc).__name__}: {exc}'
                sources[native_entry.key] = native_entry
                result.edges.append({'from':url,'to':native_entry.url,'kind':'native_entry_error_delegated',
                                     'error':f'{type(exc).__name__}: {exc}'})
                continue
            result.errors.append(f"{url}: {type(exc).__name__}: {exc}")
            # A stale career path can be repaired only by following links from the official homepage.
            if depth == 0:
                parsed = urlsplit(company.careers_url)
                homepage = f"{parsed.scheme}://{parsed.netloc}/"
                if homepage not in seen:
                    queue.append((homepage, 1))
                    result.edges.append({"from": company.careers_url, "to": homepage, "kind": "official_homepage_recovery"})
    result.sources = list(sources.values())
    if queue:
        result.warnings.append("discovery_page_budget_exhausted")
    if not result.sources and result.pages:
        result.sources = [Source("html", company.careers_url, evidence_url=company.careers_url)]
        result.warnings.append("custom_site_requires_coverage_validation")
    return result
