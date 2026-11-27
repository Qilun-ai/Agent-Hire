"""Atlassian's own server-rendered careers listing and /details/<ID> journey.

Membership authority is the unfiltered SSR HTML at
``https://www.atlassian.com/company/careers/all-jobs``: 17 department tables
exposing exact ``/company/careers/details/<ID>`` links, with an advertised
``342 jobs`` count and no job pager.  Destinations are the exact detail URLs
published by that page only; application CTAs (iCIMS) are retained as links
but never requested, and the company_sites.py hardcoded legacy endpoint is
not treated as source proof (it has no literal reference in the SSR HTML).
"""
import asyncio
import hashlib
import re
import time
from urllib.parse import urlsplit, urljoin

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

DETAIL_PATH = re.compile(r"^/company/careers/details/(\d+)$")
DETAIL_ABSOLUTE = re.compile(r"^https://www\.atlassian\.com/company/careers/details/(\d+)$")
ENTRY_PATH = "/company/careers/all-jobs"
JOBS_COUNT = re.compile(r"(\d[\d,]*)\s*jobs\b")

# Detail body leaf-block headings that are repeated company template material,
# not role content.  The role sections (Your Future Org/Team, Responsibilities,
# Qualifications, compensation/pay-zone text) are retained verbatim.
TEMPLATE_HEADINGS = {
    "working at atlassian",
    "benefits & perks",
    "benefits and perks",
    "about atlassian",
}
QUALIFICATION_HEADINGS = {
    "minimum qualifications",
    "required qualifications",
    "preferred qualifications",
    "qualifications",
    "basic qualifications",
}
ROLE_HEADINGS = {
    "responsibilities", "compensation", "your future org", "your future team",
} | QUALIFICATION_HEADINGS

# Repeated company boilerplate sentences published without a heading of their
# own (or concatenated after one).  Leaves starting with one of these are
# template material even when no <b> heading leaf precedes them.  Heading
# matching is exact-only: "About Atlassian India" is role content, not the
# "About Atlassian" template section.
TEMPLATE_LEAF_PREFIXES = (
    "atlassians can choose where they work",
    "atlassian's can choose where they work",
    "atlassians have the flexibility to work where they can collaborate",
    "as a distributed-first company, all interviews are conducted virtually",
    "whatever your preference",
    "atlassian offers a wide range of perks and benefits",
    "atlassian offers a variety of perks and benefits",
    "our perks & benefits",
    "at atlassian, we're motivated by a common goal",
    "we believe that the unique contributions of all atlassians create our success",
    "to provide you the best experience, we can support with accommodations",
    "to provide you with the best experience",
    "to provide you the best experience, we can support you with accommodations",
    "to learn more about our culture and hiring process, visit",
    "in line with local law, identity verification",
)


def _norm(text):
    return " ".join((text or "").split()).strip().rstrip(":").strip().lower()


def _heading_for(leaf):
    """Return (heading_norm, heading_label, remainder_lines) for a heading leaf.

    Exact heading leaves count for both template and role headings.  Combined
    role heading+paragraph leaves (e.g. "Your Future Org To be a 100-year
    company ...") also count for role headings, with the remainder treated as
    the section's first content part.
    """
    lines = [line.strip() for line in leaf.get_text("\n", strip=True).splitlines() if line.strip()]
    text = " ".join(lines)
    normalized = _norm(text)
    if normalized in TEMPLATE_HEADINGS or normalized in ROLE_HEADINGS:
        return normalized, text, []
    for heading in sorted(ROLE_HEADINGS, key=len, reverse=True):
        if normalized.startswith(heading + " "):
            remainder = text[len(heading):].strip()
            return heading, text[:len(heading)], ([remainder] if remainder else [])
    return None, None, []


def _boilerplate_label(leaf):
    """Template-heading label for a heading-led boilerplate leaf, else True it
    is a bare boilerplate leaf, else False.  Heading concatenated with its
    boilerplate sentence in the same leaf (e.g. "Benefits and Perks Atlassian
    offers a wide range ...") is attributed to that template heading."""
    labels = {
        "working at atlassian": "Working at Atlassian",
        "benefits & perks": "Benefits & Perks",
        "benefits and perks": "Benefits and Perks",
        "about atlassian": "About Atlassian",
    }
    normalized = _norm(leaf.get_text("\n", strip=True))
    for heading, label in labels.items():
        if normalized.startswith(heading + " "):
            remainder = normalized[len(heading) + 1:]
            if remainder.startswith(TEMPLATE_LEAF_PREFIXES):
                return label
    return normalized.startswith(TEMPLATE_LEAF_PREFIXES)


def _is_boilerplate_leaf(leaf):
    return bool(_boilerplate_label(leaf))


def _splice_template_tail(leaf):
    """Split a leaf that appends a template section after role content.

    The template marker can be a <b> heading, a plain-text heading node, or
    a bare boilerplate paragraph node concatenated after role content inside
    one leaf.  Returns ("prefix", clone) holding only the role prefix,
    ("template", label) when the leaf starts with the marker, or None.
    """
    import copy as _copy
    import re as _re
    from bs4.element import NavigableString as _NS

    label_patterns = [
        _re.compile(r"about\s+atlassian", _re.I),
        _re.compile(r"benefits\s*&\s*perks", _re.I),
        _re.compile(r"benefits\s+and\s+perks", _re.I),
        _re.compile(r"our\s+perks\s*&\s*benefits", _re.I),
        _re.compile(r"working\s+at\s+atlassian", _re.I),
    ]

    def _marker_at(node):
        raw = str(node)
        normalized = _norm(raw)
        if normalized.startswith(TEMPLATE_LEAF_PREFIXES):
            return 0
        for pattern in label_patterns:
            for match in pattern.finditer(raw):
                offset = match.start()
                if offset == 0:
                    remainder = _norm(raw[match.end():])
                    if not remainder or remainder.startswith(TEMPLATE_LEAF_PREFIXES):
                        return 0
                    continue
                if raw[offset - 1] in ".!?":
                    remainder = _norm(raw[match.end():])
                    if not remainder or remainder.startswith(TEMPLATE_LEAF_PREFIXES):
                        return offset
        return None

    clone = _copy.deepcopy(leaf)
    nodes = [node for node in clone.find_all(string=True) if str(node).strip()]
    for position, node in enumerate(nodes):
        offset = _marker_at(node)
        if offset is None:
            continue
        raw = str(node)
        if position == 0 and offset == 0:
            return "template", _norm(raw)[:60]
        rest = list(node.next_elements)
        prefix = raw[:offset]
        if prefix.strip():
            node.replace_with(_NS(prefix))
        else:
            node.extract()
        for element in reversed(rest):
            if element.parent is not None:
                element.extract()
        if not clone.get_text(strip=True):
            return "template", _norm(raw)[:60]
        return "prefix", clone
    return None



def detail_url_for(job_id):
    return f"https://www.atlassian.com/company/careers/details/{job_id}"


def parse_listing_html(html, entry_url):
    """Scope-guarded parse of the unfiltered SSR listing page.

    Only exact ``/company/careers/details/<ID>`` links on the official host
    are accepted as membership.  Returns counts, grouped rows in page order,
    facet counts, and a pager-observation flag.
    """
    parsed = urlsplit(str(entry_url))
    if (parsed.scheme != "https" or parsed.hostname not in {"atlassian.com", "www.atlassian.com"}
            or parsed.path.rstrip("/") != ENTRY_PATH):
        raise ValueError(f"Atlassian listing entry is not the official HTTPS all-jobs page: {entry_url}")
    soup = BeautifulSoup(html or "", "lxml")
    main = soup.find("main") or soup

    match = JOBS_COUNT.search(main.get_text(" ", strip=True))
    advertised = None
    if match:
        advertised = int(match.group(1).replace(",", ""))

    # Department facet counts, e.g. "Analytics & Data Science (15)", scoped to
    # the "WHAT FUNCTION MATCHES YOU?" fieldset only.
    facet_counts = {}
    for fieldset in soup.select("fieldset"):
        legend = fieldset.find("legend")
        legend_text = legend.get_text(" ", strip=True) if legend else ""
        if "WHAT FUNCTION" not in legend_text.upper():
            continue
        for chunk in fieldset.get_text(" | ", strip=True).split("|"):
            match = re.fullmatch(r"\s*(.+?)\s*\((\d+)\)\s*", chunk)
            if match and match.group(1).strip().upper() not in legend_text.upper():
                facet_counts[match.group(1).strip()] = int(match.group(2))

    rows = []
    groups = []
    seen = set()
    duplicates = []
    for heading in soup.find_all("h3"):
        department = heading.get_text(" ", strip=True)
        container = heading.parent
        group_rows = []
        for table in (container.find_all("table") if container else []):
            for link in table.select('a[href*="/company/careers/details/"]'):
                raw_href = (link.get("href") or "").strip()
                absolute = urljoin(str(entry_url), raw_href)
                found = DETAIL_ABSOLUTE.fullmatch(absolute)
                if not found:
                    continue
                job_id = found.group(1)
                row = link.find_parent("tr")
                cells = row.find_all("td") if row else []
                listing_location = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
                entry = {
                    "job_id": job_id,
                    "title": link.get_text(" ", strip=True),
                    "url": absolute,
                    "department": department,
                    "listing_location": listing_location or None,
                }
                group_rows.append(entry)
                if job_id in seen:
                    duplicates.append(job_id)
                    continue
                seen.add(job_id)
                rows.append(entry)
        if group_rows:
            groups.append({"department": department, "count": len(group_rows),
                           "job_ids": [row["job_id"] for row in group_rows]})

    pager_hits = soup.select('[class*="pagination"], [class*="pager"], [data-testid*="pagination"]')
    load_more = [button for button in soup.find_all("button")
                 if re.search(r"load\s*more|view\s*more|show\s*more", button.get_text(" ", strip=True), re.I)]
    has_pager = bool(pager_hits or load_more)
    return {
        "advertised_count": advertised,
        "rows": rows,
        "groups": groups,
        "facet_counts": facet_counts,
        "duplicate_ids": duplicates,
        "has_pager": has_pager,
        "pager_details": {
            "pagination_control_count": len(pager_hits),
            "load_more_button_count": len(load_more),
        },
    }


def _leaf_blocks(container):
    """Leaf-block nodes in document order: divs with no child divs."""
    return [node for node in container.find_all("div")
            if node.find("div") is None and node.get_text("\n", strip=True).strip()]


def _leaf_groups(body):
    """Leaf blocks grouped by their top-level section container.

    Group boundaries prevent a template heading (e.g. Working at Atlassian)
    from absorbing the following role content that has no heading of its own.
    """
    inner = next((child for child in body.find_all(recursive=False) if child.name == "div"), None)
    if inner is None:
        return [[leaf] for leaf in _leaf_blocks(body)]
    section_zone = next((child for child in inner.find_all(recursive=False) if child.name == "div"), None)
    if section_zone is None:
        return [[leaf] for leaf in _leaf_blocks(body)]
    groups = []
    for section in section_zone.find_all(recursive=False):
        leaves = [node for node in section.find_all("div") if node.find("div") is None]
        leaves = [leaf for leaf in leaves if leaf.get_text("\n", strip=True).strip()]
        if leaves:
            groups.append(leaves)
    if not groups:
        return [[leaf] for leaf in _leaf_blocks(body)]
    return groups


def _body_container(main):
    """The direct main child holding the role body (not header/apply/talent)."""
    candidates = list(main.find_all(recursive=False))
    for candidate in candidates:
        text = candidate.get_text(" ", strip=True)
        if len(text) < 300:
            continue
        if "Join the Atlassian Talent Community" in text:
            continue
        if candidate.find("h1") is not None:
            continue
        return candidate
    return None


def parse_detail_html(html, expected_id, detail_url):
    """Parse one exact SSR detail page into retained role sections.

    Excludes repeated template sections (Working at Atlassian, Benefits,
    About Atlassian) plus the Talent Community / Apply call-to-action blocks.
    Unknown dates, employer and type fields stay None when not exposed.
    """
    soup = BeautifulSoup(html or "", "lxml")
    main = soup.find("main")
    if main is None:
        raise ValueError(f"Atlassian detail {expected_id} has no <main> job body")
    heading = main.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title:
        raise ValueError(f"Atlassian detail {expected_id} has no <h1> job title")

    EMPLOYMENT_TYPES = {"full-time", "part-time", "contract", "contractor",
                        "temporary", "internship", "intern"}
    employment_type = None
    department = None
    location = None
    locations = []
    workplace = None
    for paragraph in main.select("p"):
        text = paragraph.get_text(" ", strip=True)
        if "|" in text and len(text) < 400:
            parts = [part.strip() for part in text.split("|")]
            if len(parts) >= 2 and parts[0]:
                header_meta = text
                department = parts[0]
                rest = parts[1:]
                if rest and rest[-1].lower() in EMPLOYMENT_TYPES:
                    employment_type = rest[-1]
                    rest = rest[:-1]
                locations = [part for part in rest if part]
                location = locations[0] if locations else None
                break

    # Apply CTAs: exact iCIMS links labelled "Apply for this job", in DOM order.
    apply_urls = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if "icims.com" in href and anchor.get_text(" ", strip=True) == "Apply for this job" \
                and href not in apply_urls:
            apply_urls.append(href)

    body = _body_container(main)
    if body is None:
        raise ValueError(f"Atlassian detail {expected_id} has no retained role-body container")
    groups = _leaf_groups(body)
    leaves = [leaf for group in groups for leaf in group]
    if not leaves:
        raise ValueError(f"Atlassian detail {expected_id} retained body has no leaf blocks")

    # Split leaf blocks into headed segments.  Group boundaries reset the
    # current segment so a template heading never absorbs the following
    # role content that has no heading of its own.  Boilerplate leaves and
    # mid-leaf template tails are excluded by content, since some postings
    # publish them without (or concatenated after) a heading leaf.
    segments = []
    for group in groups:
        current = None
        in_template = False
        for leaf in group:
            heading_norm, heading_label, remainder = _heading_for(leaf)
            if heading_norm in TEMPLATE_HEADINGS:
                excluded_labels = heading_label
                in_template = True
                segments.append({"heading": excluded_labels, "heading_norm": heading_norm,
                                 "blocks": [], "heading_leaf": None, "prefix_lines": [],
                                 "template": True})
                current = segments[-1]
                continue
            if heading_norm is not None:
                in_template = False
                heading_leaf = leaf
                if remainder:
                    spliced = _splice_template_tail(leaf)
                    if spliced is not None:
                        if spliced[0] == "prefix":
                            heading_leaf = spliced[1]
                        elif spliced[0] == "template":
                            in_template = True
                            segments.append({"heading": spliced[1], "heading_norm": spliced[1],
                                             "blocks": [], "heading_leaf": None,
                                             "prefix_lines": [], "template": True})
                            current = segments[-1]
                            continue
                current = {"heading": heading_label, "heading_norm": heading_norm,
                           "blocks": [], "heading_leaf": heading_leaf,
                           "prefix_lines": list(remainder), "template": False}
                segments.append(current)
                continue
            boiler_label = _boilerplate_label(leaf)
            if boiler_label:
                if isinstance(boiler_label, str):
                    segments.append({"heading": boiler_label,
                                     "heading_norm": boiler_label.lower(),
                                     "blocks": [], "heading_leaf": None,
                                     "prefix_lines": [], "template": True})
                    current = segments[-1]
                continue
            spliced = _splice_template_tail(leaf)
            if spliced is not None and spliced[0] == "prefix":
                in_template = True
                # Role prefix retained in the current segment; the template
                # tail and everything after it in this group is dropped.
                if current is None:
                    current = {"heading": None, "heading_norm": None, "blocks": [],
                               "heading_leaf": None, "prefix_lines": [], "template": False}
                    segments.append(current)
                current["blocks"].append(spliced[1])
                continue
            if spliced is not None and spliced[0] == "template":
                in_template = True
                segments.append({"heading": spliced[1], "heading_norm": spliced[1],
                                 "blocks": [], "heading_leaf": None, "prefix_lines": [],
                                 "template": True})
                current = segments[-1]
                continue
            if in_template:
                continue
            if current is None:
                current = {"heading": None, "heading_norm": None, "blocks": [leaf],
                           "heading_leaf": None, "prefix_lines": [], "template": False}
                segments.append(current)
            else:
                current["blocks"].append(leaf)

    retained_leaves = []
    excluded = []
    section_texts = {}
    responsibilities = None
    qualification_parts = []
    for segment in segments:
        heading_norm = segment["heading_norm"]
        if segment.get("template"):
            excluded.append(segment["heading"])
            continue
        retained = []
        if segment["heading_leaf"] is not None:
            retained.append(segment["heading_leaf"])
        retained.extend(segment["blocks"])
        retained_leaves.extend(retained)
        heading_label = segment["heading"]
        content_lines = list(segment.get("prefix_lines") or [])
        content_lines.extend(
            leaf.get_text("\n", strip=True)
            for leaf in segment["blocks"] if leaf.get_text("\n", strip=True).strip()
        )
        content = "\n".join(content_lines)
        if heading_norm == "responsibilities":
            responsibilities = content or None
            section_texts["responsibilities"] = content
        elif heading_norm in QUALIFICATION_HEADINGS:
            qualification_parts.append(
                f"{heading_label}\n{content}" if content else heading_label
            )
            section_texts[heading_norm] = content
        elif heading_label is not None:
            section_texts.setdefault(heading_norm, []).append(content) if isinstance(
                section_texts.get(heading_norm), list) else section_texts.__setitem__(heading_norm, content)

    retained_html = "\n".join(str(leaf) for leaf in retained_leaves)
    retained_text = text_html(retained_html)

    # Explicit compensation: pay-zone intro through Zone C, verbatim.
    block_texts = [leaf.get_text("\n", strip=True) for leaf in retained_leaves]
    block_texts = [text for text in block_texts if text.strip()]
    salary = None
    intro_index = next(
        (index for index, text in enumerate(block_texts)
         if "three geographic pay zones" in text),
        next((index for index, text in enumerate(block_texts) if "pay zones" in text), None),
    )
    if intro_index is not None:
        zones = [block_texts[intro_index]]
        for following in block_texts[intro_index + 1:]:
            zones.append(following)
            if following.strip().startswith("Zone C"):
                break
        salary = {
            "source_text": "\n".join(zones),
            "source_field": "detail_pay_zone_blocks",
        }
    return {
        "title": title,
        "header_meta": header_meta,
        "department": department,
        "location": location,
        "locations": [{"location": entry} for entry in locations],
        "workplace": workplace,
        "employment_type": employment_type,
        "apply_urls": apply_urls,
        "apply_url": apply_urls[0] if len(apply_urls) == 1 else None,
        "retained_html": retained_html,
        "retained_text": retained_text,
        "retained_block_count": len(retained_leaves),
        "excluded_template_headings": excluded,
        "responsibilities": responsibilities,
        "qualifications": "\n\n".join(qualification_parts) if qualification_parts else None,
        "salary": salary,
    }


def body_hash(html):
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


class Atlassian(Adapter):
    """Follow only the SSR listing and the exact detail URLs it publishes."""

    VERIFICATION_REVISION = "atlassian.ssr-listing-and-detail-retained-sections.2"
    DESCRIPTION_METHOD = "atlassian.official_careers_ssr.detail_body_retained_sections_v1"
    DETAIL_BATCH = 12

    async def _listing_snapshot(self, entry_url, label):
        response = await self.http.get(entry_url)
        if str(response.url) not in {entry_url, entry_url.rstrip("/") + "/"} and \
                urlsplit(str(response.url)).path.rstrip("/") != ENTRY_PATH:
            raise ValueError(f"Atlassian {label} listing request redirected away from the official all-jobs page")
        parsed = parse_listing_html(response.text, str(response.url))
        if parsed["advertised_count"] != len(parsed["rows"]):
            raise ValueError(
                f"Atlassian {label} listing advertises {parsed['advertised_count']} jobs "
                f"but exposes {len(parsed['rows'])} unique exact detail IDs"
            )
        if parsed["duplicate_ids"]:
            raise ValueError(f"Atlassian {label} listing repeats exact detail IDs: {parsed['duplicate_ids']}")
        facet_total = sum(parsed["facet_counts"].values()) if parsed["facet_counts"] else None
        if facet_total is not None and facet_total != parsed["advertised_count"]:
            raise ValueError(
                f"Atlassian {label} listing facet total {facet_total} differs from advertised {parsed['advertised_count']}"
            )
        return response, parsed

    async def run(self):
        started = self.result._started_clock
        source_started = time.perf_counter()
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        try:
            initial_response, initial = await self._listing_snapshot(entry_url, "initial")
        except Exception as exc:
            self.result.errors.append(f"Atlassian initial SSR listing failed: {type(exc).__name__}: {exc}")
            return self.result
        source_seconds = time.perf_counter() - source_started
        initial_rows = initial["rows"]
        if not initial_rows:
            self.result.expected_total = initial["advertised_count"] or 0
            self.result.errors.append("Atlassian official listing returned zero visible jobs")
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": str(initial_response.url),
                "membership_authority": "Atlassian official unfiltered SSR HTML at /company/careers/all-jobs; exact /company/careers/details/<ID> links grouped under department tables",
                "initial_advertised_count": initial["advertised_count"],
                "initial_department_groups": initial["groups"],
                "initial_has_pager": initial["has_pager"],
                "initial_pager_details": initial["pager_details"],
                "source_snapshot_phase_seconds": round(source_seconds, 6),
            })
            return self.result

        # Fetch only the exact detail URLs the entry page published.
        attempted = {}
        failed = {}
        waf_error = None
        detail_started = time.perf_counter()
        detail_seconds = 0.0
        batches = [initial_rows[index:index + self.DETAIL_BATCH]
                   for index in range(0, len(initial_rows), self.DETAIL_BATCH)]
        for batch in batches:
            if waf_error is not None:
                break
            batch_started = time.perf_counter()

            async def fetch(row):
                return await self.http.get(row["url"])

            results = await asyncio.gather(
                *(fetch(row) for row in batch), return_exceptions=True
            )
            detail_seconds += time.perf_counter() - batch_started
            for row, outcome in zip(batch, results):
                job_id = row["job_id"]
                if isinstance(outcome, Exception):
                    message = f"{type(outcome).__name__}: {outcome}"
                    failed[job_id] = message
                    if re.search(r"HTTP\s+(401|403|429)\b", message):
                        waf_error = f"Atlassian detail {row['url']} stopped collection: {message}"
                        self.result.errors.append(waf_error)
                    continue
                response = outcome
                if str(response.url) != row["url"]:
                    failed[job_id] = f"detail request redirected: {row['url']} -> {response.url}"
                    continue
                attempted[job_id] = response.text
        self.result.timings["detail_phase_seconds"] = round(time.perf_counter() - detail_started, 6)

        # Final membership snapshot (fresh fetch, never the archived body).
        final_snapshot_verified = True
        final_response = None
        final = None
        try:
            final_response, final = await self._listing_snapshot(entry_url, "final")
        except Exception as exc:
            final_snapshot_verified = False
            self.result.errors.append(
                f"Atlassian final SSR membership snapshot failed; initial rows retained: {type(exc).__name__}: {exc}"
            )
        initial_ids = [row["job_id"] for row in initial_rows]
        final_ids = [row["job_id"] for row in final["rows"]] if final else []
        changed = bool(final_snapshot_verified and initial_ids != final_ids)
        if changed:
            self.result.errors.append("atlassian_official_listing_membership_changed_between_initial_and_final_snapshots")

        # Map fetched details to jobs, in initial page order.
        mapping_started = time.perf_counter()
        made = 0
        for row in initial_rows:
            job_id = row["job_id"]
            if job_id in attempted:
                try:
                    parsed = parse_detail_html(attempted[job_id], job_id, row["url"])
                except Exception as exc:
                    self.result.errors.append(f"{job_id}: detail parse failed: {type(exc).__name__}: {exc}")
                    continue
                raw = {
                    "listing": row,
                    "detail_header_meta": parsed["header_meta"],
                    "retained_block_count": parsed["retained_block_count"],
                    "excluded_template_headings": parsed["excluded_template_headings"],
                    "section_texts": {
                        key: value for key, value in {
                            "responsibilities": parsed["responsibilities"],
                            "qualifications": parsed["qualifications"],
                        }.items() if value is not None
                    },
                    "apply_urls": parsed["apply_urls"],
                    "source_ui": {
                        "native_detail_url": row["url"],
                        "listing_department": row["department"],
                        "listing_location": row["listing_location"],
                        "listing_title": row["title"],
                        "detail_h1_title": parsed["title"],
                        "detail_department": parsed["department"],
                        "detail_location": parsed["location"],
                        "detail_workplace": parsed["workplace"],
                    },
                }
                job = self.job(
                    raw,
                    title=parsed["title"],
                    url=row["url"],
                    job_id=job_id,
                    apply_url=parsed["apply_url"],
                    description=parsed["retained_html"],
                    method=self.DESCRIPTION_METHOD,
                    description_url=row["url"],
                    locations=parsed["locations"] or (
                        [{"location": row["listing_location"]}] if row["listing_location"] else []),
                    department=parsed["department"],
                    employment_type=parsed["employment_type"],
                    workplace_type=parsed["workplace"],
                    date_posted=None,
                    date_posted_raw=None,
                    date_updated=None,
                    salary=parsed["salary"],
                    responsibilities=parsed["responsibilities"],
                    qualifications=parsed["qualifications"],
                )
                job.description_text = parsed["retained_text"]
                job.record_identity = f"atlassian:{job_id}"
                if len(parsed["apply_urls"]) != 1:
                    job.warnings.append("multiple_company_apply_links_retained_in_raw")
                if not parsed["retained_text"].strip():
                    self.result.errors.append(f"{job_id}: retained description is empty")
                self.add(job)
                made += 1
            elif job_id in failed:
                self.result.errors.append(f"{job_id}: detail fetch failed: {failed[job_id]}")
            elif waf_error is not None:
                self.result.errors.append(f"{job_id}: not attempted after WAF stop")
        mapping_seconds = time.perf_counter() - mapping_started
        missed_final_ids = sorted(set(final_ids) - set(initial_ids)) if final else []
        for identity in missed_final_ids:
            self.result.errors.append(f"{identity}: published by final listing but absent from initial detail pull")
        self.result.timings["detail_mapping_seconds"] = round(mapping_seconds, 6)
        self.result.expected_total = len(final["rows"]) if final else len(initial_rows)
        self.result.pages = 2 if final_snapshot_verified else 1
        if changed or waf_error is not None or failed or len(attempted) != len(initial_rows) or not final_snapshot_verified:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        if self.options.max_jobs and len(self.result.jobs) >= self.options.max_jobs:
            self.result.listing_complete = False
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(initial_response.url),
            "final_entry_url": str(final_response.url) if final_response else None,
            "membership_authority": "Atlassian official unfiltered SSR HTML at /company/careers/all-jobs; exact /company/careers/details/<ID> links grouped under department tables",
            "ui_pagination": "No job pager observed in SSR HTML; all exact detail links are rendered on the single entry page",
            "initial_advertised_count": initial["advertised_count"],
            "final_advertised_count": final["advertised_count"] if final else None,
            "initial_department_groups": initial["groups"],
            "final_department_groups": final["groups"] if final else None,
            "initial_facet_counts": initial["facet_counts"],
            "initial_has_pager": initial["has_pager"],
            "final_has_pager": final["has_pager"] if final else None,
            "initial_pager_details": initial["pager_details"],
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "final_snapshot_verified": final_snapshot_verified,
            "membership_changed_between_snapshots": changed,
            "detail_fetch_attempted": len(attempted),
            "detail_fetch_failed": failed,
            "detail_fetch_unattempted_after_waf": sorted(
                set(initial_ids) - set(attempted) - set(failed)
            ) if waf_error is not None else [],
            "waf_stop": waf_error,
            "description_source": "Each exact detail page's <main> role-body leaf blocks; template Working at Atlassian/Benefits/About headings excluded; Talent Community and Apply blocks excluded; retained heading/plain sections preserved verbatim",
            "salary_policy": "Explicit pay-zone block text (pay-zones intro through Zone C) preserved verbatim; no salary inferred when absent",
            "date_policy": "No reliable posting/update date is exposed on the SSR detail page; all date fields remain null",
            "employer_policy": "No explicit legal-employer field is exposed on the SSR detail page; employer remains null",
            "source_snapshot_phase_seconds": round(source_seconds, 6),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["source_snapshot_phase_seconds"] = round(source_seconds, 6)
        return self.result
