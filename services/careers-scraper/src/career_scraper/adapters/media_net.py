"""Media.net's own WordPress careers site: department listings, sitemaps and job pages.

Membership is the union of the company's home-page department cards, the
department taxonomy sitemap and the department links the job pages themselves
publish. Each department page server-renders its complete ``ul.openings-list``
(no pagination or filters), and every posting is a server-rendered detail page on
the same host. There is no ATS: the apply control is an on-page modal form.
"""
from __future__ import annotations

import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.media.net/"
ORIGIN = "https://careers.media.net"
DEPARTMENTS_SITEMAP = ORIGIN + "/wp-sitemap-taxonomies-departments-1.xml"
JOBS_SITEMAP = ORIGIN + "/wp-sitemap-posts-jobs-1.xml"
POSTS_SITEMAP = ORIGIN + "/wp-sitemap-posts-post-1.xml"
HOST = "careers.media.net"
EMP_LABEL = "Media.net"

DEPARTMENT_PATH = re.compile(r"^/([a-z0-9][a-z0-9-]*)/$")
JOB_PATH = re.compile(r"^/([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]*)/$")
POSITION_LABEL = re.compile(r"^(\d+)\s+Positions?$", re.I)
SALARY_RANGE = re.compile(r"Base\s+Compensation\s+Range\s*:\s*([^\n]+)", re.I)
TITLE_LOCATION = re.compile(r"\(([^()]{2,40}?)\s+based\)", re.I)

# The theme appends this identical 901-character block inside div.post-body on
# every posting. It is site-wide promotional copy, not the posting description.
BENEFITS_BLOCK_HEADING = "Benefits & Perks"
BENEFITS_BLOCK_CHARS = 884
BENEFITS_BLOCK_SHA256 = "ebb5d4e325a22bb34b983fd52ecc121e9aca6a47cf7f860df45bac6fe9d81a33"

RESPONSIBILITY_HEADING = re.compile(
    r"(responsibilit|what you will do|what your day|roles?\s*(?:and|&)\s*responsibilit|key duties|your impact)",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"(qualification|requirement|what we look for|what we(?:'|’)re looking for|desired skills|"
    r"technical skills|good to have|soft skills|nice to have|must[- ]have|experience)",
    re.I,
)
NON_ROLE_HEADING = re.compile(
    r"^(about|job summary|job description|compensation|benefits|perks|why join|equal opportunity|"
    r"eeo|apply|location\s*:|role\s*:|function\s*:|current openings)",
    re.I,
)


def normalized_text(value: str | None) -> str:
    value = unicodedata.normalize("NFC", (value or "").replace("\xa0", " "))
    return re.sub(r"\s+", " ", value).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_text(soup.get_text(" ", strip=True))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _https_host(url: str, expected_path: re.Pattern | None, *, exact_path: str | None = None) -> re.Match | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HOST or parts.query or parts.fragment:
        return None
    if exact_path is not None:
        return exact_path if parts.path == exact_path else None  # type: ignore[return-value]
    if expected_path is None:
        return None
    return expected_path.fullmatch(parts.path)


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the company's own home page department cards and their published counts."""
    if _https_host(response_url, None, exact_path="/") is None:
        raise ValueError("Media.net official entry is not the inspected careers home page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Careers@Media.Net" not in title:
        raise ValueError("Media.net official entry no longer identifies its careers site")
    site_name = soup.select_one('meta[property="og:site_name"]')
    if not site_name or normalized_text(site_name.get("content")) != "Media.net Careers":
        raise ValueError("Media.net official entry site identity changed")
    section = soup.find("section", id="openings")
    if section is None:
        raise ValueError("Media.net official entry no longer publishes the Open Positions section")
    cards, issues, seen = [], [], set()
    for item in section.select("div.flex-item.department-item"):
        heading = item.select_one("h3.flex-item-title")
        anchor = item.select_one("a.flex-btn-link[href]")
        if heading is None or anchor is None:
            issues.append("department_card_missing_title_or_link")
            continue
        label = normalized_text(heading.get_text(" ", strip=True))
        href = anchor.get("href", "")
        match = DEPARTMENT_PATH.fullmatch(href if href.startswith("/") else "/" + href.strip("/") + "/")
        if match is None:
            issues.append(f"department_card_link_not_a_company_department:{href}")
            continue
        slug = match[1]
        if slug in seen:
            issues.append(f"department_card_duplicate:{slug}")
            continue
        seen.add(slug)
        count_text = normalized_text(anchor.get_text(" ", strip=True))
        count = POSITION_LABEL.fullmatch(count_text)
        cards.append({
            "slug": slug,
            "url": ORIGIN + f"/{slug}/",
            "label": label,
            "advertised_count": int(count[1]) if count else None,
            "advertised_label": count_text,
        })
        if count is None:
            issues.append(f"department_card_unreadable_count:{slug}")
    if not cards:
        raise ValueError("Media.net official entry publishes no department cards")
    marketing = soup.find("a", href=lambda value: isinstance(value, str) and value.strip("/").lower() == "marketing")
    return {
        "url": response_url,
        "page_title": title,
        "site_name": normalized_text(site_name.get("content")),
        "departments": cards,
        "department_count": len(cards),
        "advertised_total": sum(card["advertised_count"] or 0 for card in cards),
        "marketing_department_linked_from_home": bool(marketing),
        "issues": issues,
    }


def parse_department_page(html: str, response_url: str, slug: str) -> dict:
    """Read one server-rendered department listing, including its zero-job state."""
    if _https_host(response_url, None, exact_path=f"/{slug}/") is None:
        raise ValueError(f"Media.net department page {slug} is not the inspected company route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Careers@Media.Net" not in title:
        raise ValueError(f"Media.net department page {slug} identity changed")
    body = soup.select_one("div.container > div.section-content-wrapper") or soup
    label_node = body.select_one("h2.heading#jobProfile")
    label = normalized_text(label_node.get_text(" ", strip=True)) if label_node else ""
    if not label:
        raise ValueError(f"Media.net department page {slug} no longer publishes its department name")
    openings_heading = next(
        (normalized_text(node.get_text(" ", strip=True)) for node in body.select("h2.heading")
         if not node.get("id") and normalized_text(node.get_text(" ", strip=True)).lower().startswith("current openings")),
        None,
    )
    rows, issues, seen = [], [], set()
    lists = soup.select("ul.openings-list")
    if len(lists) > 1:
        issues.append(f"department_multiple_openings_lists:{len(lists)}")
    for anchor in soup.select("ul.openings-list a[href]"):
        target = anchor.get("href", "")
        match = _https_host(target, JOB_PATH)
        if match is None or match[1] != slug:
            issues.append(f"department_job_link_outside_department:{target[:160]}")
            continue
        url = ORIGIN + match[0]
        if url in seen:
            issues.append(f"department_duplicate_job_link:{url}")
            continue
        seen.add(url)
        rows.append({
            "url": url,
            "slug": match[2],
            "title": normalized_text(anchor.get_text(" ", strip=True)),
            "department_slug": slug,
            "department_label": label,
        })
    active = [
        normalized_text(a.get_text(" ", strip=True))
        for a in soup.select("div.sidebar a.categories.active")
    ]
    pagination = [
        normalized_text(node.get_text(" ", strip=True))
        for node in soup.select("a,button")
        if re.search(r"\b(?:next|previous|load more|show more)\b", node.get_text(" ", strip=True), re.I)
    ]
    sidebar = [
        {"label": normalized_text(a.get_text(" ", strip=True)), "url": a.get("href", "")}
        for a in soup.select("div.sidebar a.categories[href]")
    ]
    return {
        "url": response_url,
        "slug": slug,
        "page_title": title,
        "label": label,
        "openings_heading": openings_heading,
        "rows": rows,
        "count": len(rows),
        "active_department_link": active,
        "sidebar_departments": sidebar,
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_sitemap_urls(xml: str, *, kind: str) -> list[dict]:
    """Parse one official WordPress sitemap (jobs, department taxonomy or posts)."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Media.net {kind} sitemap is not valid XML: {exc}") from exc
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag != f"{namespace}urlset":
        raise ValueError(f"Media.net {kind} sitemap is no longer a urlset")
    entries = []
    for node in root.findall(f"{namespace}url"):
        loc = node.findtext(f"{namespace}loc") or ""
        lastmod = node.findtext(f"{namespace}lastmod")
        entries.append({"url": loc.strip(), "lastmod": (lastmod or "").strip() or None})
    if not entries:
        raise ValueError(f"Media.net {kind} sitemap is unexpectedly empty")
    return entries


def parse_jobs_sitemap(xml: str) -> list[dict]:
    entries = parse_sitemap_urls(xml, kind="jobs")
    for entry in entries:
        match = _https_host(entry["url"], JOB_PATH)
        if match is None:
            raise ValueError(f"Media.net jobs sitemap contains a non-job URL: {entry['url']}")
        entry["department_slug"], entry["slug"] = match[1], match[2]
        if entry["lastmod"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", entry["lastmod"]):
            raise ValueError(f"Media.net jobs sitemap lastmod is not an ISO UTC timestamp: {entry['lastmod']}")
    return entries


def parse_departments_sitemap(xml: str) -> list[dict]:
    entries = parse_sitemap_urls(xml, kind="departments")
    for entry in entries:
        match = _https_host(entry["url"], DEPARTMENT_PATH)
        if match is None:
            raise ValueError(f"Media.net department sitemap contains a non-department URL: {entry['url']}")
        entry["slug"] = match[1]
    return entries


def parse_posts_sitemap(xml: str) -> list[dict]:
    entries = parse_sitemap_urls(xml, kind="posts")
    for entry in entries:
        match = _https_host(entry["url"], JOB_PATH)
        if match is None:
            raise ValueError(f"Media.net posts sitemap contains an unexpected URL: {entry['url']}")
        entry["department_slug"], entry["slug"] = match[1], match[2]
    return entries


def _is_role_name(heading: str, job_title: str) -> bool:
    head, title = heading.casefold(), job_title.casefold()
    if not head or not title:
        return False
    return head in title or title in head or head.split(",")[0].strip() in title


def collect_bullets(html: str) -> list[str]:
    """Collect the employer's bullet points, keeping wrapped lines with their bullet."""
    soup = BeautifulSoup(html or "", "lxml")
    items = [normalized_text(node.get_text(" ", strip=True)) for node in soup.find_all("li")]
    items = [item for item in items if item]
    if items:
        return items
    for br in soup.find_all("br"):
        br.replace_with("\n")
    collected: list[str] = []
    for raw_line in soup.get_text("", strip=False).splitlines():
        line = normalized_text(raw_line)
        if not line:
            continue
        if "•" in line:
            for index, piece in enumerate(line.split("•")):
                piece = piece.strip()
                if piece and index > 0:
                    collected.append(piece)
        elif collected:
            collected[-1] = f"{collected[-1]} {line}".strip()
    return collected


def sectionize(html: str) -> list[dict]:
    """Split the employer's post body on its heading tags and bold-only paragraphs."""
    soup = BeautifulSoup(f'<div data-media-net-body="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-media-net-body]")
    sections: list[dict] = []
    current: dict | None = None
    for node in (root.contents if root else []):
        if not isinstance(node, Tag):
            continue
        heading, body_html = None, ""
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = normalized_text(node.get_text(" ", strip=True))
        elif node.name == "p":
            first = node.find(["strong", "b"], recursive=False)
            if first is not None:
                bold = normalized_text(first.get_text(" ", strip=True))
                if bold and len(bold) <= 140:
                    heading = bold.rstrip(":").strip()
                    body_html = "".join(str(sibling) for sibling in first.next_siblings)
        if heading:
            if current is not None:
                sections.append(current)
            current = {
                "heading": heading,
                "html": body_html,
                "text": text_html(body_html),
                "bullets": collect_bullets(body_html),
            }
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": "", "bullets": []}
        current["html"] += str(node)
    if current is not None:
        sections.append(current)
    for section in sections:
        if not section["text"]:
            section["text"] = text_html(section["html"])
        if not section["bullets"]:
            section["bullets"] = collect_bullets(section["html"])
    return sections


def extract_role_fields(sections: list[dict], job_title: str) -> dict:
    """Conservative responsibilities/qualifications split; the full body is always kept."""
    responsibilities: list[str] = []
    qualifications: list[str] = []
    kind: str | None = None
    for section in sections:
        heading = section["heading"] or ""
        compact = normalized_text(heading).rstrip(":").strip()
        bullets, text = section["bullets"], section["text"].strip()
        if compact and NON_ROLE_HEADING.search(compact):
            kind = None
        elif compact and RESPONSIBILITY_HEADING.search(compact):
            kind = "responsibility"
        elif compact and QUALIFICATION_HEADING.search(compact):
            kind = "qualification"
        elif compact and _is_role_name(compact, job_title):
            if kind is None and re.search(r"\bresponsible for\b", text, re.I):
                kind = "responsibility"
        elif compact and not bullets:
            kind = None
        if kind == "responsibility":
            if bullets:
                responsibilities.extend(bullets)
            elif len(text) >= 40:
                responsibilities.append(text)
        elif kind == "qualification":
            if bullets:
                qualifications.extend(bullets)
            elif len(text) >= 40:
                qualifications.append(text)
    return {
        "responsibilities": list(dict.fromkeys(responsibilities)),
        "qualifications": list(dict.fromkeys(qualifications)),
    }


def _labelled_value(lines: list[str], label: str) -> str | None:
    pattern = re.compile(rf"^{label}\s*:\s*(.*)$", re.I)
    for index, line in enumerate(lines):
        match = pattern.match(line.strip())
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return value
        for following in lines[index + 1:]:
            candidate = following.strip()
            if not candidate:
                continue
            if re.match(r"^(?:Role|Location|Function)\s*:", candidate, re.I):
                return None
            return candidate
    return None


def parse_salary(text: str) -> dict | None:
    match = SALARY_RANGE.search(text)
    if not match:
        return None
    raw = normalized_text(match.group(1))
    amounts = [int(value.replace(",", "")) for value in re.findall(r"\$([\d,]+)", raw)]
    if len(amounts) < 1:
        return {"raw": raw, "min": None, "max": None, "currency": None}
    return {
        "raw": raw,
        "min": min(amounts),
        "max": max(amounts),
        "currency": "USD",
        "source": "description:Base Compensation Range",
    }


def _split_template_benefits(post_body: Tag) -> tuple[str, dict]:
    """Remove only the exact, byte-identical site-wide Benefits & Perks block."""
    marker = next(
        (node for node in post_body.find_all("h3", recursive=False)
         if normalized_text(node.get_text(" ", strip=True)) == BENEFITS_BLOCK_HEADING),
        None,
    )
    info = {
        "marker_found": marker is not None,
        "removed": False,
        "chars": None,
        "sha256": None,
        "reason": "no_site_template_marker",
    }
    if marker is None:
        return "".join(str(node) for node in post_body.contents if isinstance(node, Tag)), info
    block_nodes = []
    for sibling in marker.next_siblings:
        if isinstance(sibling, Tag):
            block_nodes.append(sibling)
    block_html = "".join(str(node) for node in block_nodes)
    block_text = normalized_body_text(block_html)
    info.update(chars=len(block_text), sha256=sha256(block_text.encode()).hexdigest())
    if len(block_text) == BENEFITS_BLOCK_CHARS and info["sha256"] == BENEFITS_BLOCK_SHA256:
        info.update(removed=True, reason="known_site_template_block")
    else:
        info.update(reason="template_block_changed_not_removed")
    if info["removed"]:
        info["text"] = block_text
        kept = [str(node) for node in post_body.contents if isinstance(node, Tag)]
        # Drop the marker heading and every following sibling, keep the rest.
        kept = []
        reached = False
        for node in post_body.contents:
            if not isinstance(node, Tag):
                continue
            if node is marker:
                reached = True
            if not reached:
                kept.append(str(node))
        return "".join(kept), info
    return "".join(str(node) for node in post_body.contents if isinstance(node, Tag)), info


def parse_detail_page(html: str, response_url: str, *, expected_title: str,
                      expected_department: str, expected_slug: str) -> dict:
    """Parse one exact company detail page and require it to be the listed posting."""
    match = _https_host(response_url, JOB_PATH)
    if match is None or match[1] != expected_department or match[2] != expected_slug:
        raise ValueError("Media.net detail URL is not the exact published job route")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = soup.select_one("h2.heading#jobProfile")
    title = normalized_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        raise ValueError("Media.net detail page no longer publishes its job title")
    if normalized_text(expected_title) and title.casefold() != normalized_text(expected_title).casefold():
        raise ValueError(f"Media.net detail title changed: {title!r} != {expected_title!r}")
    post_body = soup.select_one("div.post-body")
    if post_body is None:
        raise ValueError("Media.net detail page no longer publishes its post body")
    description_html, template = _split_template_benefits(post_body)
    description_text = normalized_body_text(description_html)
    if len(description_text) < 200:
        raise ValueError("Media.net detail description is empty or truncated")
    # Keep line breaks for the employer's Role/Location/Function labels and salary line.
    field_text = BeautifulSoup(description_html, "lxml").get_text("\n", strip=True)
    flat_lines = [normalized_text(line) for line in field_text.splitlines() if normalized_text(line)]
    location_value = _labelled_value(flat_lines, "Location")
    function_value = _labelled_value(flat_lines, "Function")
    role_value = _labelled_value(flat_lines, "Role")
    locations, location_source = [], None
    if location_value:
        locations = [part for part in (normalized_text(piece) for piece in re.split(r"\s*(?:&|,|/)\s*", location_value)) if part]
        location_source = "description:Location"
    else:
        title_location = TITLE_LOCATION.search(title)
        if title_location:
            locations = [normalized_text(title_location.group(1))]
            location_source = "job_title"
    apply_form = soup.select_one("form#rsvp-form")
    apply_control = soup.select_one("a.apply-btn")
    apply_meta = {
        "apply_control": normalized_text(apply_control.get_text(" ", strip=True)) if apply_control else None,
        "apply_control_has_href": bool(apply_control.get("href")) if apply_control else None,
        "form_action": None,
        "form_endpoint": None,
        "hidden_post_id": None,
        "job_profile_field": None,
        "recaptcha_sitekey": None,
    }
    if apply_form is not None:
        apply_meta.update(
            form_action=apply_form.get("action") or None,
            form_endpoint=apply_form.get("data-url") or None,
            hidden_post_id=(apply_form.select_one('input[name="post_id"]') or {}).get("value")
            if apply_form.select_one('input[name="post_id"]') else None,
            job_profile_field=bool(apply_form.select_one('input[name="rsvp[job-profile]"]')),
        )
        recaptcha = soup.select_one("div.g-recaptcha")
        apply_meta["recaptcha_sitekey"] = recaptcha.get("data-sitekey") if recaptcha else None
    sidebar = [
        {"label": normalized_text(a.get_text(" ", strip=True)), "url": a.get("href", "")}
        for a in soup.select("div.sidebar a.categories[href]")
    ]
    canonical = soup.select_one('meta[property="og:url"]')
    json_ld = []
    for node in soup.find_all("script", type="application/ld+json"):
        json_ld.append(normalized_body_text(node.string or "")[:200])
    sections = sectionize(description_html)
    role_fields = extract_role_fields(sections, title)
    return {
        "url": response_url,
        "page_title": page_title,
        "title": title,
        "canonical_url": canonical.get("content") if canonical else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_method": "html:div.post-body (site-wide Benefits & Perks template block removed)",
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "template_block": template,
        "locations": locations,
        "location_source": location_source,
        "role": role_value,
        "function": function_value,
        "salary": parse_salary(field_text),
        "responsibilities": role_fields["responsibilities"],
        "qualifications": role_fields["qualifications"],
        "section_headings": [section["heading"] for section in sections if section["heading"]],
        "apply": apply_meta,
        "sidebar_departments": sidebar,
        "json_ld_blocks": json_ld,
        "pagination_controls": [
            normalized_text(node.get_text(" ", strip=True))
            for node in soup.select("a,button")
            if re.search(r"\b(?:next|previous|load more|show more)\b", node.get_text(" ", strip=True), re.I)
        ],
    }


class MediaNet(Adapter):
    """Read Media.net's own careers home, department listings, sitemaps and detail pages."""

    VERIFICATION_REVISION = "media-net.wordpress-departments.1"

    async def _fetch(self, url: str) -> dict:
        if not self.page_guard():
            raise ValueError("Media.net page budget exhausted")
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        return {
            "response": response,
            "seconds": seconds,
            "sha256": sha256(response.content).hexdigest(),
            "bytes": len(response.content),
            "status": response.status_code,
            "url": str(response.url),
        }

    async def _probe(self, url: str) -> dict:
        """Fetch an index candidate that may legitimately be gone (404/410)."""
        try:
            fetched = await self._fetch(url)
            return {"url": fetched["url"], "status": fetched["status"],
                    "seconds": round(fetched["seconds"], 6), "sha256": fetched["sha256"]}
        except FetchError as exc:
            match = re.search(r"HTTP (\d{3})", str(exc))
            return {"url": url, "status": int(match[1]) if match else None,
                    "seconds": None, "sha256": None, "error": str(exc)}

    def _stub(self, row: dict, *, membership_source: str, lastmod: str | None) -> None:
        self.add(self.job(
            {
                "listing": row,
                "membership_source": membership_source,
                "date_updated_source": "jobs_sitemap_lastmod" if lastmod else None,
                "job_id_source": "detail_url_slug",
                "requisition_id_source": "not_published_by_company",
            },
            title=row["title"],
            url=row["url"],
            job_id=row["slug"],
            department=row["department_label"],
            date_posted=None,
            date_posted_raw=None,
            date_updated=lastmod,
            apply_url=row["url"],
        ))

    async def detail(self, job):
        fetched = await self._fetch(job.url)
        response = fetched["response"]
        listing = job.raw["listing"]
        parsed = parse_detail_page(
            response.text,
            fetched["url"],
            expected_title=listing["title"],
            expected_department=listing["department_slug"],
            expected_slug=listing["slug"],
        )
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = parsed["description_method"]
        job.description_source_url = job.url
        job.apply_url = job.url
        job.locations = parsed["locations"]
        job.team = parsed["function"]
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"] or None
        job.qualifications = parsed["qualifications"] or None
        job.employer = EMP_LABEL
        job.date_posted = None
        job.raw.update({
            "detail": {
                "request_url": job.url,
                "final_url": fetched["url"],
                "status": fetched["status"],
                "request_seconds": round(fetched["seconds"], 6),
                "response_sha256": fetched["sha256"],
                "response_bytes": fetched["bytes"],
                "page_title": parsed["page_title"],
                "canonical_url": parsed["canonical_url"],
                "template_block": parsed["template_block"],
                "location_source": parsed["location_source"],
                "role": parsed["role"],
                "function": parsed["function"],
                "apply": parsed["apply"],
                "section_headings": parsed["section_headings"],
                "sidebar_departments": parsed["sidebar_departments"],
                "json_ld_blocks": parsed["json_ld_blocks"],
                "pagination_controls": parsed["pagination_controls"],
                "description_normalized_chars": parsed["description_normalized_chars"],
                "description_fnv1a64": parsed["description_fnv1a64"],
            }
        })
        if parsed["template_block"].get("marker_found") and not parsed["template_block"].get("removed"):
            job.warnings.append("benefits_template_block_changed")
        if not parsed["locations"]:
            job.warnings.append("location_not_published")
        return job

    async def run(self):
        started = self.result._started_clock
        if not self.page_guard():
            return self.result
        entry_fetch = await self._fetch(OFFICIAL_ENTRY)
        entry = parse_official_entry(entry_fetch["response"].text, entry_fetch["url"])

        department_sitemap_fetch = await self._fetch(DEPARTMENTS_SITEMAP)
        department_sitemap = parse_departments_sitemap(department_sitemap_fetch["response"].text)
        department_order: list[str] = []
        for card in entry["departments"]:
            if card["slug"] not in department_order:
                department_order.append(card["slug"])
        for item in department_sitemap:
            if item["slug"] not in department_order:
                department_order.append(item["slug"])

        department_pages: dict[str, dict] = {}
        department_fetch: dict[str, dict] = {}
        for slug in department_order:
            fetched = await self._fetch(f"{ORIGIN}/{slug}/")
            page = parse_department_page(fetched["response"].text, fetched["url"], slug)
            page.update(status=fetched["status"], seconds=round(fetched["seconds"], 6),
                        response_sha256=fetched["sha256"], response_bytes=fetched["bytes"])
            department_pages[slug] = page
            department_fetch[slug] = fetched
            for issue in page["issues"]:
                self.result.errors.append(f"{slug}: {issue}")

        jobs_sitemap_fetch = await self._fetch(JOBS_SITEMAP)
        jobs_sitemap = parse_jobs_sitemap(jobs_sitemap_fetch["response"].text)
        sitemap_by_url = {item["url"]: item for item in jobs_sitemap}

        rows: list[dict] = []
        seen_urls: set[str] = set()
        listing_issues: list[str] = []
        for slug in department_order:
            page = department_pages[slug]
            for row in page["rows"]:
                if row["url"] in seen_urls:
                    listing_issues.append(f"duplicate_company_job_url_across_departments:{row['url']}")
                    continue
                seen_urls.add(row["url"])
                rows.append({**row, "membership_source": "department_listing"})
        for item in jobs_sitemap:
            if item["url"] in seen_urls:
                continue
            listing_issues.append(f"jobs_sitemap_url_missing_from_department_listings:{item['url']}")
            seen_urls.add(item["url"])
            rows.append({
                "url": item["url"],
                "slug": item["slug"],
                "title": "",
                "department_slug": item["department_slug"],
                "department_label": department_pages.get(item["department_slug"], {}).get("label")
                or item["department_slug"].replace("-", " ").title(),
                "membership_source": "jobs_sitemap_only",
            })

        # A stale post-type sitemap entry is probed; a live one is a real posting.
        stale_entries = []
        posts_sitemap_fetch = await self._fetch(POSTS_SITEMAP)
        for item in parse_posts_sitemap(posts_sitemap_fetch["response"].text):
            if item["url"] in seen_urls:
                continue
            match = _https_host(item["url"], JOB_PATH)
            if match is None or match[1] not in department_order:
                continue
            probe = await self._probe(item["url"])
            status = probe["status"]
            stale_entries.append({"url": item["url"], "status": status, "reason": "post_sitemap_not_in_department_listing"})
            if status == 200:
                seen_urls.add(item["url"])
                rows.append({
                    "url": item["url"],
                    "slug": match[2],
                    "title": "",
                    "department_slug": match[1],
                    "department_label": department_pages.get(match[1], {}).get("label")
                    or match[1].replace("-", " ").title(),
                    "membership_source": "posts_sitemap_live",
                })
            elif status not in (404, 410):
                self.result.errors.append(f"stale_sitemap_probe_failed:{item['url']}:HTTP {status}")

        lastmod_by_url = {url: item["lastmod"] for url, item in sitemap_by_url.items()}
        for row in rows:
            self._stub(row, membership_source=row["membership_source"], lastmod=lastmod_by_url.get(row["url"]))
        expected_total = len(rows)
        if len(self.result.jobs) != expected_total:
            listing_issues.append(f"unique_source_jobs_{expected_total}_but_adapter_jobs_{len(self.result.jobs)}")
        listing_wall_seconds = max(0.0, time.perf_counter() - started)

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_wall_seconds = max(0.0, time.perf_counter() - detail_started)

        # Every job page publishes its department navigation; fetch any department
        # that was not already discovered, then hydrate only the new rows.
        known = set(department_pages)
        sidebar_extra: list[str] = []
        for _ in range(2):
            discovered: list[str] = []
            for job in self.result.jobs:
                for link in (job.raw.get("detail") or {}).get("sidebar_departments") or []:
                    target = link.get("url", "")
                    match = _https_host(target, DEPARTMENT_PATH)
                    if match and match[1] not in known and match[1] not in discovered:
                        discovered.append(match[1])
            if not discovered:
                break
            new_rows = []
            for slug in discovered:
                sidebar_extra.append(slug)
                known.add(slug)
                fetched = await self._fetch(f"{ORIGIN}/{slug}/")
                page = parse_department_page(fetched["response"].text, fetched["url"], slug)
                page.update(status=fetched["status"], seconds=round(fetched["seconds"], 6),
                            response_sha256=fetched["sha256"], response_bytes=fetched["bytes"])
                department_pages[slug] = page
                for row in page["rows"]:
                    if row["url"] in seen_urls:
                        continue
                    seen_urls.add(row["url"])
                    new_rows.append({**row, "membership_source": "detail_sidebar_department"})
            for row in new_rows:
                self._stub(row, membership_source=row["membership_source"], lastmod=lastmod_by_url.get(row["url"]))
            new_jobs = [job for job in self.result.jobs if not job.description_html]
            if new_jobs:
                extra_started = time.perf_counter()
                await self.hydrate(new_jobs, self.detail)
                detail_wall_seconds += max(0.0, time.perf_counter() - extra_started)
            expected_total = len(rows) + len(new_rows) if not sidebar_extra else len(seen_urls)
            rows.extend(new_rows)

        jobs_by_url = {job.url: job for job in self.result.jobs}
        department_totals = {
            slug: {
                "label": page["label"],
                "url": page["url"],
                "status": page["status"],
                "rows": page["count"],
                "advertised_count": next(
                    (card["advertised_count"] for card in entry["departments"] if card["slug"] == slug), None
                ),
                "response_sha256": page["response_sha256"],
            }
            for slug, page in department_pages.items()
        }
        home_counts_match = all(
            item["advertised_count"] is None or item["advertised_count"] == item["rows"]
            for item in department_totals.values()
        )
        if not home_counts_match:
            listing_issues.append("home_grid_advertised_counts_differ_from_department_pages")
        listing_set = {row["url"] for row in rows}
        sitemap_set = set(sitemap_by_url)
        if listing_set != sitemap_set:
            listing_issues.append(
                f"listing_vs_jobs_sitemap_difference:listing_only={sorted(listing_set - sitemap_set)}:"
                f"sitemap_only={sorted(sitemap_set - listing_set)}"
            )
        missing_from_home = sorted(set(department_pages) - {card["slug"] for card in entry["departments"]})
        detail_failures = [
            job for job in self.result.jobs
            if not job.description_verified
        ]
        all_departments_fetched = all(page["status"] == 200 for page in department_pages.values())
        self.result.expected_total = expected_total
        self.result.listing_complete = bool(
            all_departments_fetched
            and len(self.result.jobs) == expected_total
            and not self.result.errors
            and not any("duplicate_company_job_url" in issue for issue in listing_issues)
        )
        details_seconds = sum(
            (job.raw.get("detail") or {}).get("request_seconds") or 0.0 for job in self.result.jobs
        )
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry": {
                "status": entry_fetch["status"],
                "final_url": entry_fetch["url"],
                "response_sha256": entry_fetch["sha256"],
                "request_seconds": round(entry_fetch["seconds"], 6),
                "department_count": entry["department_count"],
                "advertised_total": entry["advertised_total"],
                "page_title": entry["page_title"],
            },
            "listing_authority": (
                "Media.net's own server-rendered department pages (home department cards + "
                "department taxonomy sitemap + per-job department sidebar), cross-checked "
                "against the WordPress jobs sitemap"
            ),
            "membership": {
                "departments": department_totals,
                "department_count": len(department_totals),
                "departments_missing_from_home_grid": missing_from_home,
                "home_grid_advertised_total": entry["advertised_total"],
                "home_grid_counts_match_department_pages": home_counts_match,
                "jobs_sitemap_url": JOBS_SITEMAP,
                "jobs_sitemap_count": len(jobs_sitemap),
                "jobs_sitemap_matches_listing": listing_set == sitemap_set,
                "listing_union_count": expected_total,
                "jobs_collected": len(self.result.jobs),
                "sitemap_only_rows": [row["url"] for row in rows if row["membership_source"] != "department_listing"],
                "detail_sidebar_extra_departments": sidebar_extra,
                "stale_sitemap_entries": stale_entries,
                "listing_issues": listing_issues,
                "pagination": "none: every department page renders its complete list",
            },
            "dates": {
                "date_posted": "not published on home, department, detail or sitemap; stays null",
                "date_updated": "WordPress jobs-sitemap <lastmod> only (source dates are 2024-07-02..2025-12-08)",
                "valid_through": "not published; stays null",
            },
            "description_source": "server-rendered div.post-body on each exact company detail page",
            "template_benefits_block": {
                "heading": BENEFITS_BLOCK_HEADING,
                "normalized_chars": BENEFITS_BLOCK_CHARS,
                "normalized_sha256": BENEFITS_BLOCK_SHA256,
                "policy": "removed only when the exact site-wide block hash matches; original HTML retained in responses",
            },
            "apply_mechanism": (
                "on-page #rsvp-form modal posting to /wp-admin/admin-ajax.php (action=rsvp_submit); "
                "no external ATS; hidden post_id is the same 8420 on every job and is not a job id"
            ),
            "detail_failures": [
                {"id": job.job_id, "url": job.url, "warnings": job.warnings}
                for job in detail_failures
            ],
            "jobs_by_url": len(jobs_by_url),
            "request_seconds_sum": {
                "entry": round(entry_fetch["seconds"], 6),
                "department_sitemap": round(department_sitemap_fetch["seconds"], 6),
                "department_pages": round(sum(item["seconds"] for item in department_fetch.values()), 6),
                "jobs_sitemap": round(jobs_sitemap_fetch["seconds"], 6),
                "posts_sitemap": round(posts_sitemap_fetch["seconds"], 6),
                "detail_fetch_wall_concurrency_2": round(details_seconds, 6),
            },
            "phase_wall_seconds": {
                "listing_and_sitemaps": round(listing_wall_seconds, 6),
                "detail_hydration": round(detail_wall_seconds, 6),
                "post_detail_reconciliation": round(
                    max(0.0, time.perf_counter() - started - listing_wall_seconds - detail_wall_seconds), 6
                ),
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        return self.result
