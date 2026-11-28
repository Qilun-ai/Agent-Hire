"""LetsTransport's official careers journey: WordPress hiring page + published static openings.

The ledger entry ``https://letstransport.in/careers/`` is a 404. The current
official careers page is ``https://letstransport.in/we-are-hiring/`` (linked as
"We are hiring" from the site header). That page currently publishes no vacancy
rows: its "Open opportunities" wrapper is empty, the WordPress ``openings`` post
type has zero public records, and the only hiring call-to-action is a resume
email. Separately, the company publishes static opening pages under the official
``https://letstransport.in/openings/`` directory; the Apache auto-index is the
complete public enumeration of those pages. At review time it contained exactly
one company-authored posting (Area Sales Manager), which this adapter collects in
full. No ATS, aggregator or guessed board is used. If WordPress openings or the
directory index gain entries that this flow did not inspect, the listing is
marked incomplete instead of silently dropping them.
"""
from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from html import escape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


REQUESTED_ENTRY = "https://letstransport.in/careers/"
OFFICIAL_CAREERS = "https://letstransport.in/we-are-hiring/"
OPENINGS_INDEX = "https://letstransport.in/openings/"
WP_OPENINGS_API = "https://letstransport.in/wp-json/wp/v2/openings?per_page=100&_fields=id,title,link"
SITE_HOST = "letstransport.in"
RECRUITMENT_EMAIL = "recruitment@letstransport.team"
JOB_LINK = re.compile(r"/(?:openings?|jobs?|positions?|careers?)(?:/|$)", re.I)
INDEX_TITLE = re.compile(r"^Index of /openings/?$", re.I)


def _same_site(url: str, path_prefix: str | None = None, exact_path: str | None = None) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != SITE_HOST:
        return False
    if exact_path is not None and parsed.path.rstrip("/") != exact_path.rstrip("/"):
        return False
    if path_prefix is not None and not parsed.path.startswith(path_prefix):
        return False
    return not parsed.query and not parsed.fragment


def _js_literal(html: str, name: str):
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*(\[.*?\]);", html, re.S)
    if match is None:
        raise ValueError(f"LetsTransport opening page is missing its {name} content array")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"LetsTransport {name} content array is not valid JSON") from exc
    return value


def _pair_list(value, name: str, width: int):
    if not isinstance(value, list) or not value or any(
            not isinstance(row, list) or len(row) != width for row in value):
        raise ValueError(f"LetsTransport {name} content array changed shape")
    return value


def parse_careers_page(html: str, response_url: str = OFFICIAL_CAREERS) -> dict:
    if not _same_site(response_url, exact_path="/we-are-hiring"):
        raise ValueError("LetsTransport careers response came from an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "hiring" not in title.casefold() or "letstransport" not in title.casefold():
        raise ValueError("LetsTransport careers page identity changed")
    heading = next(
        (node for node in soup.select("h1,h2,h3")
         if node.get_text(" ", strip=True).casefold() == "open opportunities"),
        None,
    )
    if heading is None:
        raise ValueError("LetsTransport 'Open opportunities' section is missing")
    section = heading.find_parent("section")
    wrapper = section.select_one(".categories_wrapper") if section else None
    if wrapper is None:
        raise ValueError("LetsTransport 'Open opportunities' wrapper is missing")
    section_links = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in section.select("a[href]")
    ] if section else []
    job_like = sorted({link for link in section_links if JOB_LINK.search(urlsplit(link).path)})
    mailto = sorted({
        anchor.get("href", "").strip()
        for anchor in soup.select('a[href^="mailto:"]')
        if RECRUITMENT_EMAIL in anchor.get("href", "").lower()
    })
    return {
        "url": response_url,
        "title": title,
        "open_opportunities_heading": True,
        "categories_wrapper_empty": not wrapper.get_text(" ", strip=True) and not wrapper.select("a[href]"),
        "section_link_count": len(section_links),
        "job_like_links": job_like,
        "recruitment_email_links": mailto,
    }


def parse_wp_openings(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("LetsTransport WordPress openings response is not a list")
    rows = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise ValueError("LetsTransport WordPress openings response changed shape")
        title = item.get("title")
        link = item.get("link")
        if isinstance(title, dict):
            title = title.get("rendered")
        if not isinstance(title, str) or not isinstance(link, str) or not _same_site(link):
            raise ValueError("LetsTransport WordPress opening is missing its public title or link")
        rows.append({"id": item["id"], "title": text_html(title), "link": link})
    identities = [(row["id"], row["link"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("LetsTransport WordPress openings response repeats a posting")
    return rows


def parse_openings_index(html: str, response_url: str = OPENINGS_INDEX) -> dict:
    if not _same_site(response_url, exact_path="/openings"):
        raise ValueError("LetsTransport openings index response came from an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not INDEX_TITLE.match(title):
        raise ValueError("LetsTransport openings index is no longer the inspected directory listing")
    rows, seen, ignored = [], set(), []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("mailto:", "javascript:")):
            continue
        target = urljoin(response_url, href)
        parsed = urlsplit(target)
        if parsed.netloc != SITE_HOST or not parsed.path.startswith("/openings/"):
            ignored.append(href)
            continue
        if parsed.query or parsed.fragment:
            ignored.append(href)
            continue
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not parsed.path.endswith("/") or not slug or slug in {".", ".."}:
            ignored.append(href)
            continue
        if target in seen:
            continue
        seen.add(target)
        rows.append({"slug": slug, "url": target, "label": anchor.get_text(" ", strip=True)})
    return {"url": response_url, "title": title, "rows": rows, "ignored_links": ignored}


def parse_job_page(html: str, response_url: str) -> dict:
    if not _same_site(response_url, path_prefix="/openings/"):
        raise ValueError("LetsTransport opening page came from an unexpected URL")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.find("h1")
    hero_small = soup.select_one(".hero small")
    hero_summary = soup.select_one(".hero p")
    if title_node is None or hero_small is None or hero_summary is None:
        raise ValueError("LetsTransport opening hero is missing its title, context or summary")
    title = title_node.get_text(" ", strip=True)
    eyebrow = hero_small.get_text(" ", strip=True)
    summary = hero_summary.get_text(" ", strip=True)
    if not title or not summary:
        raise ValueError("LetsTransport opening title or summary is empty")

    stats = {}
    for stat in soup.select(".stat"):
        number = stat.find("b")
        label = stat.find("span")
        if number is None or label is None or not re.fullmatch(r"\d+", number.get("data-n", "")):
            raise ValueError("LetsTransport opening company stat is missing its source value")
        stats[re.sub(r"\W+", "_", label.get_text(" ", strip=True)).strip("_")] = int(number["data-n"])

    responsibilities = _pair_list(_js_literal(html, "st"), "responsibility", 2)
    measures = _pair_list(_js_literal(html, "sc"), "measure", 3)
    criteria = _pair_list(_js_literal(html, "fit"), "fit", 2)
    if not all(isinstance(row[0], str) and row[0].strip() for row in responsibilities + measures + criteria):
        raise ValueError("LetsTransport opening content has an empty heading")
    if not all(isinstance(row[1], str) for row in responsibilities):
        raise ValueError("LetsTransport responsibility text is not a string")
    if not all(isinstance(row[1], str) and isinstance(row[2], str) for row in measures):
        raise ValueError("LetsTransport measure text changed shape")
    if not all(isinstance(row[1], int) and row[1] in (0, 1) for row in criteria):
        raise ValueError("LetsTransport fit criterion must-have flag changed shape")

    email = re.search(r"const\s+EMAIL\s*=\s*\[([^\]]*)\]\.join\('@'\);", html)
    phone = re.search(r"const\s+PHONE\s*=\s*\[([^\]]*)\]\.join\(''\);", html)
    apply_href = re.search(r"const\s+APPLY_HREF\s*=\s*'([^']+)';", html)
    if not email or not phone or not apply_href:
        raise ValueError("LetsTransport opening application/contact constants are missing")
    email_parts = re.findall(r"'([^']*)'", email.group(1))
    phone_parts = re.findall(r"'([^']*)'", phone.group(1))
    contact_email = "@".join(email_parts)
    contact_phone = "".join(phone_parts)
    apply_url = apply_href.group(1).strip()
    if contact_email != RECRUITMENT_EMAIL:
        raise ValueError("LetsTransport opening recruitment email changed")
    if not apply_url.startswith("https://"):
        raise ValueError("LetsTransport opening apply destination is not https")

    about = soup.select_one("#about")
    contact = soup.select_one("#contact")
    if about is None or contact is None:
        raise ValueError("LetsTransport opening page is missing its company or contact section")
    for node in about.select(".tabNext"):
        node.decompose()
    about_heading = about.find("h2")
    if about_heading is not None:
        about_heading.decompose()
    about_html = "".join(str(child) for child in about.contents).strip()

    contact_rows = {}
    for cell in contact.select(".ct"):
        strong = cell.find("strong")
        label = cell.find("small")
        if strong is None or label is None:
            continue
        text = strong.get_text(" ", strip=True)
        if not text:
            text = {"Email": contact_email, "Mobile / WhatsApp": contact_phone}.get(
                label.get_text(" ", strip=True), "")
        contact_rows[label.get_text(" ", strip=True)] = text
    website = sorted({urljoin(response_url, a.get("href", "")) for a in contact.select("a[href]")})

    eyebrow_parts = [part.strip() for part in eyebrow.split("·") if part.strip()]
    locations = []
    experience_raw = None
    for part in eyebrow_parts:
        upper = part.upper()
        if upper.startswith("NOW HIRING"):
            continue
        if re.search(r"\byrs?\b|years?", part, re.I):
            experience_raw = part
        elif "CIT" in upper or "LOCATION" in upper or "REMOTE" in upper:
            locations.append(part.capitalize() if part.isupper() else part)
    if not locations and "MULTIPLE CITIES" in eyebrow.upper():
        locations = ["Multiple cities"]
    if not locations:
        raise ValueError("LetsTransport opening location context is missing from the hero")

    meta_rows = {
        meta.get("property"): meta.get("content")
        for meta in soup.select("meta[property^='og:']")
    }
    if meta_rows.get("og:url") and meta_rows["og:url"].rstrip("/") != response_url.rstrip("/"):
        raise ValueError("LetsTransport opening Open Graph URL does not match its page")

    def itemized(rows, body_key="body"):
        return [
            f"<li><strong>{escape(str(row[0]))}</strong> — {escape(str(row[1]))}</li>"
            if body_key == "body" else
            f"<li><strong>{escape(str(row[0]))}</strong> — {escape(str(row[1]))}. {escape(str(row[2]))}</li>"
            for row in rows
        ]

    criteria_items = [
        f"<li>{escape(str(row[0]))}{' (must-have)' if row[1] else ''}</li>"
        for row in criteria
    ]
    description_html = "\n".join([
        f"<p class=\"meta\">{escape(eyebrow)}</p>",
        f"<p>{escape(summary)}</p>",
        "<h2>What You'll Do</h2>",
        "<ul>" + "".join(itemized(responsibilities)) + "</ul>",
        "<h2>How You'll Succeed</h2>",
        "<ul>" + "".join(itemized(measures, "measure")) + "</ul>",
        "<h2>Who We Are</h2>",
        about_html,
        "<h2>Is This Right for You?</h2>",
        "<ul>" + "".join(criteria_items) + "</ul>",
        "<h2>Get in Touch</h2>",
        f"<p>Email: {escape(contact_email)}<br>Mobile / WhatsApp: {escape(contact_phone)}"
        f"<br>Website: {escape(contact_rows.get('Website', 'letstransport.in'))}"
        f"<br>Headquarters: {escape(contact_rows.get('Headquarters', ''))}</p>",
    ])
    description_text = text_html(description_html)
    if len(description_text) < 500:
        raise ValueError("LetsTransport opening full description is unexpectedly short")

    return {
        "url": response_url,
        "title": title,
        "eyebrow": eyebrow,
        "summary": summary,
        "locations": locations,
        "experience": {
            "advertised": re.sub(r"\byrs?\b", "years", experience_raw, flags=re.I)
            if experience_raw else None,
            "requirement": next(
                (row[0] for row in criteria if "years" in row[0].casefold()), None),
        },
        "responsibilities": [
            {"title": row[0], "description": row[1]} for row in responsibilities
        ],
        "measures": [
            {"title": row[0], "summary": row[1], "meaning": row[2]} for row in measures
        ],
        "criteria": [
            {"requirement": row[0], "must_have": bool(row[1])} for row in criteria
        ],
        "about_html": about_html,
        "contact": {
            "email": contact_email, "phone": contact_phone,
            "website": contact_rows.get("Website"), "linkedin": contact_rows.get("LinkedIn"),
            "instagram": contact_rows.get("Instagram"),
            "headquarters": contact_rows.get("Headquarters"),
        },
        "contact_links": website,
        "apply_url": apply_url,
        "company_stats": stats,
        "og": meta_rows,
        "description_html": description_html,
        "description_text": description_text,
    }


class LetsTransport(Adapter):
    VERIFICATION_REVISION = "letstransport.official-wp-hiring-page-and-static-openings-directory.1"

    async def detail(self, job):
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("LetsTransport detail request budget exhausted")
        response = await self.http.get(job.url)
        parsed = parse_job_page(response.text, str(response.url))
        self.result.timings["detail_page_request_seconds"] = (
            self.result.timings.get("detail_page_request_seconds", 0.0)
            + max(0.0, time.perf_counter() - started)
        )
        job.title = parsed["title"]
        job.locations = parsed["locations"]
        job.experience = parsed["experience"]
        job.apply_url = parsed["apply_url"]
        job.qualifications = parsed["criteria"]
        job.responsibilities = parsed["responsibilities"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_source_url = parsed["url"]
        job.description_method = "letstransport.official_static_opening_full_page_and_inline_content_arrays"
        job.raw.update({
            "source_slug": job.raw.get("source_slug"),
            "page": {
                "eyebrow": parsed["eyebrow"], "summary": parsed["summary"],
                "measures": parsed["measures"], "about_html": parsed["about_html"],
                "contact": parsed["contact"], "contact_links": parsed["contact_links"],
                "company_stats": parsed["company_stats"], "og": parsed["og"],
                "file_last_modified": response.headers.get("last-modified"),
            },
            "source_last_modified_is_posting_date": False,
            "posting_date_published": False,
            "job_or_requisition_id_published": False,
            "salary_published": False,
            "employment_type_published": False,
            "legal_employer_published": False,
        })
        job.warnings.append("posting_date_not_published_in_source")
        return job

    async def run(self):
        started = time.perf_counter()
        listing_started = time.perf_counter()
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            requested_entry_url=REQUESTED_ENTRY,
            official_careers_url=OFFICIAL_CAREERS,
            openings_index_url=OPENINGS_INDEX,
            wordpress_openings_api=WP_OPENINGS_API,
            membership_authority="official company-domain /openings/ directory index + WordPress openings CPT cross-check",
            ats_requests=0,
            aggregator_requests=0,
        )
        if self.company.careers_url != REQUESTED_ENTRY:
            self.result.errors.append("letstransport_ledger_entry_changed_requires_review")
            self.result.expected_total = None
            self.result.listing_complete = False
            return self.result

        # 1) Requested ledger route: observed 404. Stop on WAF/rate-limit instead
        # of pretending the route is empty.
        if not self.page_guard():
            return self.result
        requested_status = None
        try:
            requested = await self.http.get(REQUESTED_ENTRY)
            requested_status = requested.status_code
        except FetchError as exc:
            message = f"{type(exc).__name__}: {exc}"
            match = re.search(r"HTTP\s+(\d{3})\b", message)
            requested_status = int(match.group(1)) if match else None
            if requested_status is None:
                self.result.errors.append(f"letstransport_requested_entry_unreachable: {message}")
                self.result.expected_total = None
                self.result.listing_complete = False
                return self.result
        self.result.metadata["requested_entry_status"] = requested_status
        if requested_status in (401, 403, 429):
            self.result.errors.append(
                f"letstransport_requested_entry_blocked_status_{requested_status}_stop_without_bypass")
            self.result.expected_total = None
            self.result.listing_complete = False
            return self.result
        if requested_status != 404:
            self.result.errors.append(
                f"letstransport_requested_entry_status_{requested_status}_requires_fresh_journey_review")

        # 2) Current official careers page.
        if not self.page_guard():
            return self.result
        careers_started = time.perf_counter()
        careers = await self.http.get(OFFICIAL_CAREERS)
        self.result.timings["careers_page_seconds"] = time.perf_counter() - careers_started
        if str(careers.url) != OFFICIAL_CAREERS:
            self.result.errors.append("letstransport_official_careers_redirected_requires_review")
            self.result.expected_total = None
            self.result.listing_complete = False
            return self.result
        careers_info = parse_careers_page(careers.text, str(careers.url))
        self.result.metadata["careers_page"] = careers_info
        if careers_info["job_like_links"]:
            self.result.errors.append(
                "letstransport_careers_page_now_links_openings_requires_fresh_listing_review")

        # 3) WordPress openings cross-check: currently zero published records.
        if not self.page_guard():
            return self.result
        wp_started = time.perf_counter()
        wp_rows = parse_wp_openings(await self.http.json(WP_OPENINGS_API))
        self.result.timings["wordpress_openings_api_seconds"] = time.perf_counter() - wp_started
        self.result.metadata["wordpress_openings"] = {
            "count": len(wp_rows), "ids": [row["id"] for row in wp_rows],
            "links": [row["link"] for row in wp_rows],
        }
        if wp_rows:
            self.result.errors.append(
                "letstransport_wordpress_openings_not_inspected_by_this_flow")

        # 4) The official static openings directory is the enumerable company
        # listing. Cross-check it before and after the detail work.
        if not self.page_guard():
            return self.result
        index_started = time.perf_counter()
        index_response = await self.http.get(OPENINGS_INDEX)
        index = parse_openings_index(index_response.text, str(index_response.url))
        self.result.timings["openings_index_seconds"] = time.perf_counter() - index_started
        self.result.metadata["openings_index"] = {
            "title": index["title"],
            "count": len(index["rows"]),
            "entries": index["rows"],
            "ignored_links": index["ignored_links"],
        }
        expected = {row["url"]: row for row in index["rows"]}
        self.result.expected_total = len(expected) + len(wp_rows)
        for row in index["rows"]:
            self.add(self.job(
                {"source_slug": row["slug"], "listing": row},
                title=row["slug"].replace("-", " ").title(),
                url=row["url"],
                job_id=None,
                record_identity=sha256(
                    f"{self.company.name}|letstransport|{row['url']}".encode()).hexdigest(),
            ))
        self.result.timings["listing_seconds"] = max(
            0.0, time.perf_counter() - listing_started)
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)

        if not self.page_guard():
            return self.result
        recheck_started = time.perf_counter()
        final = await self.http.get(OPENINGS_INDEX)
        final_index = parse_openings_index(final.text, str(final.url))
        self.result.timings["final_openings_recheck_seconds"] = time.perf_counter() - recheck_started
        final_urls = {row["url"] for row in final_index["rows"]}
        if final_urls != set(expected):
            self.result.listing_complete = False
            self.result.errors.append("letstransport_openings_index_changed_during_collection")
        else:
            self.result.listing_complete = (
                self.result.expected_total == len(self.result.jobs)
                and not self.result.errors
            )
        self.result.metadata.update(
            listing_entry_count=len(index["rows"]),
            wordpress_openings_count=len(wp_rows),
            collected_count=len(self.result.jobs),
            listing_recheck_entry_count=len(final_index["rows"]),
            listing_recheck_matches=final_urls == set(expected),
            application_route="google_form_advertised_on_the_opening_page",
            total_listing_seconds=round(max(0.0, time.perf_counter() - listing_started), 6),
        )
        self.result.metadata["collection_seconds"] = round(
            max(0.0, time.perf_counter() - started), 6)
        return self.result
