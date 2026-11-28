"""Calendly's native careers component and its exact public Greenhouse posts."""
from __future__ import annotations

from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html
from ..structured import embedded_page_object


OFFICIAL_ENTRY = "https://careers.calendly.com/"
CAREERS_HUB = "https://calendly.com/careers"
NATIVE_API_URL = "https://boards-api.greenhouse.io/v1/boards/calendly/jobs?content=true"
# Kept as the source URL name used by the parent integration. The adapter
# discovers and validates this URL from Calendly's shipped CareersHubIsland on
# every run before requesting it.
LISTING_URL = NATIVE_API_URL
DETAIL_PATH = re.compile(r"^/calendly/jobs/(\d+)$")
NATIVE_CARD_HOST = "boards.greenhouse.io"
DETAIL_HOST = "job-boards.greenhouse.io"
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)


def normalized_title(value: str | None) -> str:
    """Normalize only Unicode composition and whitespace; preserve title wording."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def rendered_text(html: str) -> str:
    """Approximate visible inline text and collapse browser whitespace for audits."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    # get_text's separator is useful between blocks, but it must not insert a
    # space between inline emphasis and adjacent punctuation (e.g. "day<strong>,").
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    result = 14695981039346656037
    for char in value:
        result = ((result ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{result:016x}"


def _context(html: str) -> dict:
    return embedded_page_object(html, "window.__remixContext = ")


def _imports(script: str) -> list[tuple[dict[str, str], str]]:
    """Return each module's local import names and published relative URL."""
    imports = []
    for binding_text, specifier in re.findall(
        r'import\{([^}]+)\}from["\']([^"\']+)["\']', script
    ):
        bindings = {}
        for binding in binding_text.split(","):
            parts = binding.strip().split(" as ")
            if len(parts) == 1:
                bindings[parts[0]] = parts[0]
            elif len(parts) == 2:
                bindings[parts[1]] = parts[0]
        imports.append((bindings, specifier))
    return imports


def parse_careers_component(html: str, response_url: str) -> str:
    """Read the CareersHubIsland module path from Calendly's official entry HTML."""
    if response_url != CAREERS_HUB:
        raise ValueError("Calendly official entry did not resolve to its inspected careers hub")
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one('main[data-testid="template-careers-listing"]') is None:
        raise ValueError("Calendly official entry no longer renders the careers listing template")
    islands = soup.select('astro-island[component-export="CareersHubIsland"]')
    if len(islands) != 1:
        raise ValueError("Calendly CareersHubIsland is missing or ambiguous in official entry HTML")
    path = islands[0].get("component-url")
    url = urljoin(CAREERS_HUB, path or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "calendly.com" or not parsed.path.startswith("/_astro/"):
        raise ValueError("Calendly official entry published an unexpected CareersHubIsland module URL")
    return url


def _export_alias(script: str, exported_name: str) -> str:
    exports = script.rsplit("export{", 1)
    if len(exports) != 2:
        raise ValueError("Calendly published module export table is missing")
    match = re.search(
        rf"(?<![\w$])([A-Za-z_$][\w$]*) as {re.escape(exported_name)}(?![\w$])", exports[1]
    )
    if match is None:
        raise ValueError(f"Calendly published module no longer exports {exported_name}")
    return match.group(1)


def derive_native_component(index_js: str, index_url: str, islands_js: str, islands_url: str,
                            shared_js: str, shared_url: str) -> dict:
    """Derive the API, card URL, and department filter from the shipped component graph."""
    component_export_local = _export_alias(index_js, "CareersHubIsland")
    index_imports = _imports(index_js)
    island_import = next(
        ((bindings, specifier) for bindings, specifier in index_imports
         if component_export_local in bindings), None
    )
    if island_import is None:
        raise ValueError("Calendly CareersHubIsland export is not linked to a published module import")
    island_bindings, island_specifier = island_import
    if urljoin(index_url, island_specifier) != islands_url:
        raise ValueError("Calendly CareersHubIsland module dependency changed")
    island_export_name = island_bindings[component_export_local]
    islands_wrapper = _export_alias(islands_js, island_export_name)
    wrapper_match = re.search(
        rf"(?<![\w$]){re.escape(islands_wrapper)}=\w+\(([A-Za-z_$][\w$]*)\)", islands_js
    )
    if wrapper_match is None:
        raise ValueError("Calendly CareersHubIsland export wrapper changed")
    islands_impl = wrapper_match.group(1)
    component_start = islands_js.find(f"{islands_impl}=")
    component_body = islands_js[component_start:component_start + 1800] if component_start >= 0 else ""
    if "Un().then" not in component_body or "Kn(t)" not in component_body:
        raise ValueError("Calendly CareersHubIsland implementation is missing from its published dependency")

    api_matches = re.findall(
        r'([A-Za-z_$][\w$]*)=`(https://boards-api\.greenhouse\.io/[^`]+)`', islands_js
    )
    api_alias, api_url = next((alias, value) for alias, value in api_matches
                              if "boards-api.greenhouse.io" in value)
    api_parts = urlsplit(api_url)
    if (api_url != NATIVE_API_URL or api_parts.scheme != "https"
            or api_parts.netloc != "boards-api.greenhouse.io"
            or api_parts.path != "/v1/boards/calendly/jobs"
            or parse_qs(api_parts.query) != {"content": ["true"]}):
        raise ValueError("Calendly CareersHubIsland published request changed from the inspected native source")
    request_re = re.compile(
        rf"fetch\({re.escape(api_alias)},\{{method:`GET`,cache:`no-store`,signal:AbortSignal\.timeout\([A-Za-z_$][\w$]*\)\}}\)"
    )
    if request_re.search(islands_js) is None:
        raise ValueError("Calendly CareersHubIsland no longer issues the inspected no-store jobs request")

    detail_matches = re.findall(
        r'([A-Za-z_$][\w$]*)=e=>`(https://boards\.greenhouse\.io/calendly/jobs/\$\{e\})`', islands_js
    )
    detail_alias, detail_template = next(iter(detail_matches), (None, None))
    if (detail_alias is None or f"applyUrl:{detail_alias}(i.id)" not in islands_js):
        raise ValueError("Calendly CareersHubIsland exact card-link template changed")
    filter_map = re.search(
        r'function\s+[A-Za-z_$][\w$]*\(e\)\{let t=([A-Za-z_$][\w$]*),n=new Set,r=new Set,',
        islands_js,
    )
    if filter_map is None:
        raise ValueError("Calendly CareersHubIsland department membership mapper changed")
    department_alias = filter_map.group(1)
    mapping_end = islands_js.find("var qn=", filter_map.start())
    mapping = islands_js[filter_map.start():mapping_end if mapping_end >= 0 else filter_map.start() + 2600]
    if ("t.includes(e)" not in mapping or "a.length===0" not in mapping
            or "Array.isArray(i.departments)" not in mapping):
        raise ValueError("Calendly CareersHubIsland no longer filters rows by its department list")

    shared_specifier = next(
        (specifier for bindings, specifier in _imports(islands_js)
         if bindings.get(department_alias) == "Ft"), None
    )
    if shared_specifier is None or urljoin(islands_url, shared_specifier) != shared_url:
        raise ValueError("Calendly CareersHubIsland's department list module changed")
    department_export = _export_alias(shared_js, "Ft")
    department_match = re.search(
        rf"\b{re.escape(department_export)}=\[([^\]]*)\]", shared_js
    )
    if department_match is None:
        raise ValueError("Calendly published department filter list is missing")
    departments = re.findall(r"`([^`]+)`", department_match.group(1))
    if not departments or len(departments) != len(set(departments)):
        raise ValueError("Calendly published department filter list is invalid")

    return {
        "component_export": "CareersHubIsland",
        "component_implementation": islands_impl,
        "api_url": api_url,
        "api_request_method": "GET",
        "api_request_cache": "no-store",
        "detail_url_template": detail_template,
        "department_order": departments,
        "component_module_url": index_url,
        "islands_module_url": islands_url,
        "department_module_url": shared_url,
    }


def _component_url(template: str, identity: str) -> str:
    if not re.fullmatch(r"https://boards\.greenhouse\.io/calendly/jobs/\$\{e\}", template):
        raise ValueError("Calendly published exact detail URL template is invalid")
    return template.replace("${e}", identity)


def parse_listing(payload: dict, department_order: list[str], detail_url_template: str,
                  request_url: str = NATIVE_API_URL) -> dict:
    """Replicate the live CareersHubIsland membership filter and department order."""
    if request_url != NATIVE_API_URL:
        raise ValueError("Calendly native jobs request came from an unexpected published URL")
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    total = meta.get("total") if isinstance(meta, dict) else None
    if (not isinstance(jobs, list) or not all(isinstance(row, dict) for row in jobs)
            or not isinstance(total, int) or total < 0):
        raise ValueError("Calendly native jobs request no longer has its published jobs/meta schema")

    allowed = set(department_order)
    rows, seen, issues, excluded = [], set(), [], []
    source_order_ids = []
    for source_row in jobs:
        identity = str(source_row.get("id", ""))
        source_order_ids.append(identity)
        departments = source_row.get("departments")
        matched_departments = (
            [item.get("name") for item in departments
             if isinstance(item, dict) and item.get("name") in allowed]
            if isinstance(departments, list) else []
        )
        if not matched_departments:
            excluded.append({
                "job_id": identity or None,
                "reason": "CareersHubIsland department filter has no matching department",
                "department_names": [item.get("name") for item in departments if isinstance(item, dict)]
                                   if isinstance(departments, list) else [],
            })
            continue

        source_url = str(source_row.get("absolute_url", ""))
        source_parts = urlsplit(source_url)
        source_match = DETAIL_PATH.fullmatch(source_parts.path)
        card_url = _component_url(detail_url_template, identity) if re.fullmatch(r"\d+", identity) else ""
        card_parts = urlsplit(card_url)
        card_match = DETAIL_PATH.fullmatch(card_parts.path)
        location = source_row.get("location")
        location_name = location.get("name") if isinstance(location, dict) else None
        if (not re.fullmatch(r"\d+", identity) or source_match is None or source_match[1] != identity
                or source_parts.scheme != "https" or source_parts.netloc != DETAIL_HOST
                or card_match is None or card_match[1] != identity or card_parts.scheme != "https"
                or card_parts.netloc != NATIVE_CARD_HOST or not normalized_title(source_row.get("title"))
                or not isinstance(location_name, str) or not location_name.strip()):
            issues.append(f"invalid_native_component_job:{identity or 'missing-id'}")
            continue
        if identity in seen:
            issues.append(f"duplicate_native_component_job_id:{identity}")
            continue
        seen.add(identity)
        metadata = source_row.get("metadata")
        is_featured = isinstance(metadata, list) and any(
            isinstance(item, dict) and item.get("name") == "Featured Job Post"
            and item.get("value") == "True" for item in metadata
        )
        rows.append({
            "source": dict(source_row),
            "native_view": {
                "id": identity,
                "title": source_row.get("title") or "",
                "locationName": location_name,
                "applyUrl": card_url,
                "departments": matched_departments,
                "isFeatured": is_featured,
                "updatedAt": source_row.get("updated_at"),
            },
        })

    if total != len(jobs):
        issues.append("native_jobs_meta_total_mismatch")

    # The component groups Featured rows first, then department groups in the
    # published shared department-list order. Within each group it keeps API
    # order; a role belonging to multiple groups is one unique exported job.
    ordered = []
    by_id = {row["native_view"]["id"]: row for row in rows}
    if any(row["native_view"]["isFeatured"] for row in rows):
        ordered.extend(row for row in rows if row["native_view"]["isFeatured"])
    present_departments = {name for row in rows for name in row["native_view"]["departments"]}
    department_filters = [name for name in department_order if name in present_departments]
    for department in department_filters:
        ordered.extend(row for row in rows if department in row["native_view"]["departments"])
    ordered_rows, ordered_ids = [], set()
    for row in ordered:
        identity = row["native_view"]["id"]
        if identity not in ordered_ids:
            ordered_rows.append(by_id[identity])
            ordered_ids.add(identity)

    location_filters = sorted({row["native_view"]["locationName"] for row in rows}, key=str.casefold)
    return {
        "rows": ordered_rows,
        "job_ids": [row["native_view"]["id"] for row in ordered_rows],
        "source_order_ids": source_order_ids,
        "count": len(ordered_rows),
        "total": total,
        "response_job_count": len(jobs),
        "issues": issues,
        "excluded_rows": excluded,
        "department_filters": department_filters,
        "department_filter_order": list(department_order),
        "location_filters": location_filters,
        "source_fields": sorted({key for row in jobs for key in row}),
    }


def _section_rows(body: Tag) -> list[dict]:
    sections = []
    for heading in body.select("h1,h2,h3,h4,h5,h6"):
        parts = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in HEADING_TAGS:
                break
            if isinstance(sibling, Tag) and sibling.find(list(HEADING_TAGS)):
                # The next section is wrapped in an element instead of being a
                # direct sibling; don't absorb it into this section.
                break
            parts.append(str(sibling))
        sections.append({
            "heading": heading.get_text(" ", strip=True),
            "html": "".join(parts),
            "text": rendered_text("".join(parts)),
        })
    return sections


def parse_detail(job, html: str, response_url: str, response_sha256: str | None = None):
    """Hydrate one exact CareersHubIsland-linked job URL from its SSR response."""
    identity = str(job.job_id or "")
    parsed = urlsplit(response_url)
    match = DETAIL_PATH.fullmatch(parsed.path)
    card = urlsplit(job.url)
    card_match = DETAIL_PATH.fullmatch(card.path)
    if (parsed.scheme != "https" or parsed.netloc != DETAIL_HOST or match is None
            or match[1] != identity or card.scheme != "https" or card.netloc != NATIVE_CARD_HOST
            or card_match is None or card_match[1] != identity):
        raise ValueError("Calendly exact job response or native card URL changed its listed posting identity")

    context = _context(html)
    loaders = context.get("state", {}).get("loaderData", {})
    route = loaders.get("routes/$url_token_.jobs_.$job_post_id")
    payload = route.get("jobPost") if isinstance(route, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Calendly exact job SSR payload is missing")
    row = job.raw.get("listing", {})
    native = job.raw.get("native_membership", {})
    location = row.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    actual_title = normalized_title(payload.get("title"))
    if (payload.get("post_type") != "job_post" or payload.get("company_name") != "Calendly"
            or actual_title != normalized_title(job.title)
            or payload.get("public_url") != row.get("absolute_url")
            or payload.get("job_post_location") != location_name
            or payload.get("published_at") != row.get("first_published")
            or payload.get("hiring_plan_id") != row.get("internal_job_id")):
        raise ValueError("Calendly detail SSR ID/title/company/location/date does not match its native component listing")

    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("main.job-post h1")
    body = soup.select_one("main.job-post .job__description")
    if (heading is None or normalized_title(heading.get_text(" ", strip=True)) != actual_title
            or body is None):
        raise ValueError("Calendly exact job title or complete description body is missing")
    body_html = str(body)
    body_text = text_html(body_html)
    normalized_body = rendered_text(body_html)
    if len(body_text) < 1000 or len(normalized_body) < 1000:
        raise ValueError("Calendly exact job description is empty or truncated")

    for field in ("introduction", "content", "conclusion"):
        fragment = payload.get(field)
        if not isinstance(fragment, str) or not fragment.strip():
            raise ValueError(f"Calendly detail source body section is missing: {field}")
        normalized_fragment = rendered_text(fragment)
        if normalized_fragment and normalized_fragment not in normalized_body:
            raise ValueError(f"Calendly rendered full body omits its SSR {field} section")

    section_rows = _section_rows(body)
    responsibility_terms = ("day in the life", "what you’ll do", "what you'll do", "responsibilit", "about the role")
    qualification_terms = ("what do we need", "who you are", "qualif", "what we’re looking for",
                           "what we're looking for", "requirements", "bonus points")
    responsibilities = [s for s in section_rows if any(term in s["heading"].lower() for term in responsibility_terms)]
    qualifications = [s for s in section_rows if any(term in s["heading"].lower() for term in qualification_terms)]

    description_links = []
    attachments = []
    for anchor in body.select("a[href]"):
        link = {"text": anchor.get_text(" ", strip=True), "url": anchor["href"]}
        description_links.append(link)
        if ATTACHMENT_SUFFIX.search(anchor["href"]):
            attachments.append(link)

    location = payload["job_post_location"]
    location_lower = location.lower()
    workplace_type = "Hybrid" if "(hybrid)" in location_lower else "Remote" if location_lower.startswith("remote") else None
    pay_ranges = payload.get("pay_ranges")
    salary_basis = "annual base salary" if "annual base salary" in normalized_body.lower() else None
    salary = {"basis": salary_basis, "ranges": pay_ranges} if isinstance(pay_ranges, list) and pay_ranges else None
    form = soup.select_one("form#application-form")
    form_action = form.get("action") if form else None
    apply_url = payload.get("public_url") if form is not None else None

    job.title = actual_title
    job.apply_url = apply_url
    job.description_html = body_html
    job.description_text = body_text
    job.description_method = "calendly.greenhouse.ssr-jobPost"
    job.description_source_url = response_url
    job.locations = [location]
    job.employer = payload.get("company_name")
    job.language = payload.get("language")
    job.workplace_type = workplace_type
    job.date_posted = payload.get("published_at") or None
    job.date_posted_raw = payload.get("published_at")
    job.valid_through = payload.get("application_deadline") or None
    job.salary = salary
    job.responsibilities = responsibilities or None
    job.qualifications = qualifications or None
    job.raw["detail"] = {
        "job_post": payload,
        "description_html": body_html,
        "description_sections": section_rows,
        "description_links": description_links,
        "role_attachments": attachments,
        "application_form_action": form_action,
        "rendered_body_normalized_chars": len(normalized_body),
        "rendered_body_fnv1a64": fnv1a64(normalized_body),
        "source_response_sha256": response_sha256,
        "native_card_url": native.get("applyUrl"),
        "response_url": response_url,
    }
    return job


class Calendly(Adapter):
    """Read the jobs exposed by Calendly's current native careers component."""

    VERIFICATION_REVISION = "calendly.careers-hub-island-native.1"

    async def _published_native_source(self):
        """Start at the official entry and derive its actual published request graph."""
        started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_seconds = max(0.0, time.perf_counter() - started)
        if str(entry.url) != CAREERS_HUB:
            raise ValueError("Calendly official entry no longer resolves to the inspected careers hub")
        component_url = parse_careers_component(entry.text, str(entry.url))

        async def load_asset(url):
            asset_started = time.perf_counter()
            response = await self.http.get(url)
            elapsed = max(0.0, time.perf_counter() - asset_started)
            if str(response.url) != url:
                raise ValueError(f"Calendly published careers module redirected unexpectedly: {url}")
            return response, elapsed

        index, index_seconds = await load_asset(component_url)
        component_export_local = _export_alias(index.text, "CareersHubIsland")
        island_import = next(
            ((bindings, specifier) for bindings, specifier in _imports(index.text)
             if component_export_local in bindings), None
        )
        if island_import is None:
            raise ValueError("Calendly published component module has no CareersHubIsland implementation import")
        _, island_specifier = island_import
        islands_url = urljoin(component_url, island_specifier)
        islands, islands_seconds = await load_asset(islands_url)
        island_exports = _imports(islands.text)
        filter_map = re.search(
            r'function\s+[A-Za-z_$][\w$]*\(e\)\{let t=([A-Za-z_$][\w$]*),n=new Set,r=new Set,',
            islands.text,
        )
        if filter_map is None:
            raise ValueError("Calendly CareersHubIsland department mapping is missing")
        department_alias = filter_map.group(1)
        island_match = next(
            (specifier for bindings, specifier in island_exports
             if bindings.get(department_alias) == "Ft"), None
        )
        if island_match is None:
            raise ValueError("Calendly CareersHubIsland has no published department-list module dependency")
        shared_url = urljoin(islands_url, island_match)
        shared, shared_seconds = await load_asset(shared_url)
        component = derive_native_component(
            index.text, component_url, islands.text, islands_url, shared.text, shared_url
        )
        timings = {
            "official_entry_http_seconds": entry_seconds,
            "component_module_http_seconds": index_seconds,
            "islands_module_http_seconds": islands_seconds,
            "department_module_http_seconds": shared_seconds,
        }
        for name, response in (("official_entry", entry), ("component_module", index),
                               ("islands_module", islands), ("department_module", shared)):
            component[name] = {
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "http_status": response.status_code,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            }
        component["request_timings_seconds"] = timings
        return component

    async def _listing_snapshot(self, native_source):
        if not self.page_guard():
            return None
        started = time.perf_counter()
        response = await self.http.get(native_source["api_url"])
        elapsed = max(0.0, time.perf_counter() - started)
        if str(response.url) != native_source["api_url"]:
            raise ValueError("Calendly native careers request redirected away from its published jobs API")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Calendly native careers request did not return JSON") from exc
        parsed = parse_listing(
            payload,
            native_source["department_order"],
            native_source["detail_url_template"],
            str(response.url),
        )
        parsed.update({
            "request_url": native_source["api_url"],
            "final_url": str(response.url),
            "http_status": response.status_code,
            "request_seconds": elapsed,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "request_method": "GET",
            "request_cache": "no-store (same uncached behavior as the published component fetch)",
        })
        return parsed

    def _make_job(self, row):
        source_row = row["source"]
        native_view = row["native_view"]
        identity = str(native_view["id"])
        location = native_view.get("locationName")
        workplace_type = ("Hybrid" if isinstance(location, str) and "(hybrid)" in location.lower()
                          else "Remote" if isinstance(location, str) and location.lower().startswith("remote")
                          else None)
        department = native_view.get("departments")
        return self.job(
            {"listing": dict(source_row), "native_membership": dict(native_view)},
            title=native_view["title"],
            url=native_view["applyUrl"],
            job_id=identity,
            requisition_id=source_row.get("requisition_id"),
            employer="Calendly",
            locations=[location] if location else [],
            department=department[0] if isinstance(department, list) and department else None,
            workplace_type=workplace_type,
            date_posted=source_row.get("first_published") or None,
            date_posted_raw=source_row.get("first_published"),
            date_updated=source_row.get("updated_at") or None,
        )

    async def _detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        fetch_seconds = max(0.0, time.perf_counter() - started)
        parsed = urlsplit(str(response.url))
        match = DETAIL_PATH.fullmatch(parsed.path)
        card = urlsplit(job.url)
        card_match = DETAIL_PATH.fullmatch(card.path)
        if (parsed.scheme != "https" or parsed.netloc != DETAIL_HOST or match is None
                or match[1] != str(job.job_id) or card.scheme != "https"
                or card.netloc != NATIVE_CARD_HOST or card_match is None
                or card_match[1] != str(job.job_id)):
            raise ValueError("Calendly exact detail URL redirected to another company or posting")
        if any(
            urlsplit(str(hop.url)).netloc != NATIVE_CARD_HOST
            or DETAIL_PATH.fullmatch(urlsplit(str(hop.url)).path) is None
            or DETAIL_PATH.fullmatch(urlsplit(str(hop.url)).path)[1] != str(job.job_id)
            for hop in response.history
        ):
            raise ValueError("Calendly exact detail response followed an unexpected redirect")
        parse_started = time.perf_counter()
        hydrated = parse_detail(job, response.text, str(response.url), sha256(response.content).hexdigest())
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        timing = self.result.metadata.setdefault("detail_source_timings", {})
        timing[str(job.job_id)] = {"http_seconds": fetch_seconds, "local_parse_seconds": parse_seconds,
                                   "response_bytes": len(response.content),
                                   "response_sha256": sha256(response.content).hexdigest(),
                                   "http_status": response.status_code, "final_url": str(response.url)}
        self.result.timings["detail_http_seconds_sum"] = self.result.timings.get("detail_http_seconds_sum", 0.0) + fetch_seconds
        self.result.timings["detail_local_parse_seconds_sum"] = self.result.timings.get("detail_local_parse_seconds_sum", 0.0) + parse_seconds
        return hydrated

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "redirected_careers_hub": CAREERS_HUB,
            "source_status": "native_component_derivation_pending",
        })
        try:
            native_source = await self._published_native_source()
        except Exception as exc:
            self.result.errors.append(f"Calendly official native source derivation failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.metadata.update({
            "native_component_provenance": native_source,
            "native_membership_method": (
                "Follow the CareersHubIsland module URL published in current official entry HTML; "
                "derive its no-store Greenhouse request, exact card URL template, department filter "
                "and display group order from the shipped component and imported shared filter module."
            ),
            "listing_url": native_source["api_url"],
            "listing_authority": "The exact GET and membership filter in Calendly's current native CareersHubIsland.",
            "listing_method": "GET the exact API URL derived from CareersHubIsland; apply its own recognized-department filter and UI grouping order.",
            "detail_method": "GET only the exact card URLs generated by CareersHubIsland for its filtered native IDs; parse matching jobPost SSR details.",
            "request_scope": "Only job IDs retained by the current CareersHubIsland department filter; unrecognized and unassigned ATS rows are excluded.",
        })
        initial = await self._listing_snapshot(native_source)
        if initial is None:
            self.result.errors.append("calendly_initial_native_listing_budget_exhausted")
            return self.result

        rows = initial["rows"]
        self.result.expected_total = initial["count"]
        self.result.metadata["initial_listing"] = initial
        self.result.metadata["department_filters"] = initial["department_filters"]
        self.result.metadata["department_filter_order"] = initial["department_filter_order"]
        self.result.metadata["location_filters"] = initial["location_filters"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])

        jobs = [self._make_job(row) for row in rows]
        if self.options.max_jobs and len(jobs) > self.options.max_jobs:
            jobs = jobs[:self.options.max_jobs]
            self.result.warnings.append("sample_limit")
            self.result.listing_complete = False

        # Reuse only a full detail verified by this exact company flow and an
        # identical complete listing row. A changed listing field invalidates it.
        old_cache = self.options.cached_jobs
        usable = {}
        by_url = {row["native_view"]["applyUrl"]: row for row in rows}
        for url, cached in old_cache.items():
            row = by_url.get(url)
            detail = cached.raw.get("detail", {}) if isinstance(cached.raw, dict) else {}
            payload = detail.get("job_post", {}) if isinstance(detail, dict) else {}
            source_row = row.get("source", {}) if isinstance(row, dict) else {}
            native_view = row.get("native_view", {}) if isinstance(row, dict) else {}
            if (row is not None and cached.description_verified
                    and cached.description_method == "calendly.greenhouse.ssr-jobPost"
                    and str(cached.job_id) == str(native_view.get("id"))
                    and normalized_title(cached.title) == normalized_title(native_view.get("title"))
                    and payload.get("public_url") == source_row.get("absolute_url")
                    and payload.get("title") == native_view.get("title")
                    and payload.get("job_post_location") == native_view.get("locationName")
                    and payload.get("published_at") == source_row.get("first_published")
                    and payload.get("hiring_plan_id") == source_row.get("internal_job_id")):
                usable[url] = cached
        self.options.cached_jobs = usable
        try:
            if jobs:
                await self.hydrate(jobs, self._detail)
        finally:
            self.options.cached_jobs = old_cache

        # A final exact same-page snapshot checks current membership and every
        # listing field after hydration. It does not follow or guess pagination.
        final = await self._listing_snapshot(native_source)
        self.result.metadata["final_listing"] = final
        if final is None:
            self.result.errors.append("calendly_final_native_listing_budget_exhausted")
            final_matches = False
        else:
            final_ids = final["job_ids"]
            initial_ids = initial["job_ids"]
            same_membership_and_order = final_ids == initial_ids
            changed_fields = {}
            initial_by_id = {row["native_view"]["id"]: row["source"] for row in initial["rows"]}
            final_by_id = {row["native_view"]["id"]: row["source"] for row in final["rows"]}
            for identity in initial_ids:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            counts_match = final["count"] == initial["count"]
            source_fields_match = final["source_fields"] == initial["source_fields"]
            final_matches = (
                same_membership_and_order and counts_match and source_fields_match
                and not changed_fields and not final["issues"]
            )
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_response_job_count": initial["response_job_count"],
                "final_response_job_count": final["response_job_count"],
                "initial_meta_total": initial["total"],
                "final_meta_total": final["total"],
                "initial_source_order_ids": initial["source_order_ids"],
                "final_source_order_ids": final["source_order_ids"],
                "same_ids_and_order": same_membership_and_order,
                "counts_match": counts_match,
                "source_fields_match": source_fields_match,
                "native_source_fields_initial": initial["source_fields"],
                "native_source_fields_final": final["source_fields"],
                "excluded_ids_initial": [row["job_id"] for row in initial["excluded_rows"]],
                "excluded_ids_final": [row["job_id"] for row in final["excluded_rows"]],
                "excluded_rows_initial": initial["excluded_rows"],
                "excluded_rows_final": final["excluded_rows"],
                "changed_listing_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final_ids if i not in initial_ids],
                "removed_job_ids": [i for i in initial_ids if i not in final_ids],
                "complete": final_matches,
            }
            if final["issues"]:
                self.result.errors.extend(final["issues"])
            if changed_fields:
                self.result.errors.append("calendly_listing_fields_changed_during_collection")
            if not same_membership_and_order or not counts_match or not source_fields_match:
                self.result.errors.append("calendly_listing_membership_changed_during_collection")

        for job in jobs:
            row = by_url.get(job.url)
            if row is not None:
                job.raw["listing"] = dict(row["source"])
                job.raw["native_membership"] = dict(row["native_view"])
        self.result.jobs = jobs
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["retained_total"] = len(jobs)
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(
            final_matches and not initial["issues"] and final is not None and not final["issues"]
            and len(rows) == initial["count"] and len(jobs) == initial["count"]
            and "sample_limit" not in self.result.warnings
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        if self.result.errors and not self.result.jobs:
            self.result.metadata["retained_total"] = 0
        return self.result
