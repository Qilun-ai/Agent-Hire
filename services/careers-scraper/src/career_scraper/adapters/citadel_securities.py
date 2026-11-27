"""Citadel Securities's own WordPress careers listing and native detail journey.

Membership authority is the official Open Opportunities page
(https://www.citadelsecurities.com/careers/open-opportunities/) and its
server-rendered ``/page/<n>/`` pagination links, which the page's own
pagination widget publishes. The page also embeds a filter form
(``#ajax-careers-search-filter``) that POSTs to
``/wp-admin/admin-ajax.php`` with ``action=careers_listing_filter``,
``per_page=10``, ``sort_order=DESC`` and ``current_page=<n>`` (see
``citadel-securities.min.js`` ``careerListingFilter``). The GET
pagination hrefs and the AJAX content return the same company-visible
cards for the same page number (spot-checked for pages 1-2 during
research); the adapter collects through the GET pagination because it
is the server-rendered HTML the entry page itself links to, without
filter parameters. No ATS board is enumerated; the only detail pages
fetched are the exact ``/careers/details/<slug>/`` hrefs on those
company-published cards.

Details are native WordPress ``single-career`` pages. Their
``form.gild-form`` carries the stable ``job_id`` (UUID) and
``career_post_id`` (WP post ID); the on-page Gild application form is
the apply surface (no external ATS URL is published on the detail
page). The role body is the HTML between the ``Job Description`` and
``About Citadel Securities`` headings inside
``.single-job-application__description .text``; the standard h2 About
boilerplate and page chrome are excluded from the retained description
but the full ``.text`` block is preserved in ``raw`` for audit. Three
postings additionally carry an author-written ``<b>About Citadel
Securities</b>`` subsection inside the Job Description; that authored
text (including the salary sentence that follows it on the hourly
contract role) is retained as published.
"""
import asyncio
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


DETAIL_PATH = re.compile(r"^/careers/details/([^/]+)/$")
PAGE_PATH = re.compile(r"^/careers/open-opportunities/page/(\d+)/$")
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
SALARY_RE = re.compile(r"base salary range for this role is [^.]*\.", re.I)
PER_WEEK_RE = re.compile(r"per week", re.I)


def compact_body_hash(html):
    """NFC/whitespace-free hash matching the parent browser-reference method."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _salary_from_description(description_text):
    match = SALARY_RE.search(description_text or "")
    if not match:
        return None
    source_text = match.group(0).strip()
    salary = {"source_text": source_text, "source_field": "description_text"}
    if PER_WEEK_RE.search(source_text):
        salary["semantics"] = (
            "Weekly internship base salary range as published; preserve source text without conversion."
        )
    else:
        salary["semantics"] = (
            "Annual base salary range as published; preserve source text without conversion."
        )
    return salary


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append(
                {"url": href, "label": anchor.get_text(" ", strip=True) or None}
            )
    return values


def _split_locations(location_text):
    locations = []
    for part in (location_text or "").split(","):
        name = " ".join(part.split())
        if name:
            locations.append({"location": name})
    return locations


def _description_body_html(text_block):
    """Return the HTML between Job Description and About headings.

    ``text_block`` is the ``.single-job-application__description .text``
    element. Raises ValueError when the observed headings are absent so
    a redesigned template fails loudly instead of storing chrome text.
    """
    headings = {}
    for heading in text_block.find_all(["h2", "h3"]):
        label = heading.get_text(" ", strip=True).lower()
        if label == "job description":
            headings.setdefault("job", heading)
        elif label == "about citadel securities":
            headings.setdefault("about", heading)
    if "job" not in headings or "about" not in headings:
        raise ValueError(
            "Citadel Securities detail no longer contains Job Description / About headings"
        )
    job_heading = headings["job"]
    about_heading = headings["about"]
    parts = []
    for node in job_heading.next_siblings:
        if node is about_heading:
            break
        if getattr(node, "name", None):
            parts.append(str(node))
        elif str(node).strip():
            parts.append(str(node))
    return "".join(parts).strip()


class CitadelSecurities(Adapter):
    """Collect Citadel Securities's own listing pages and exact detail pages."""

    VERIFICATION_REVISION = "citadel-securities.native-wp-listing-and-details.1"
    HOSTS = {"www.citadelsecurities.com"}
    AJAX_ACTION = "careers_listing_filter"
    # Cloudflare intermittently returns an interactive 403 challenge that
    # the shared HTTP client does not retry; bounded adapter-level retries
    # (without any challenge solving) are the only recovery used here.
    DETAIL_MAX_ATTEMPTS = 5

    @staticmethod
    def _assert_company_url(url, *, prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in CitadelSecurities.HOSTS:
            raise ValueError(
                f"Citadel Securities source left the official HTTPS site: {url}"
            )
        if not parsed.path.startswith(prefix):
            raise ValueError(f"Citadel Securities source has an unexpected path: {url}")
        return str(url)

    @staticmethod
    def _listing_total(soup):
        node = soup.select_one(".total-post")
        if node is None:
            raise ValueError("Citadel Securities listing has no total-post count")
        text = node.get_text(strip=True)
        if not text.isdigit():
            raise ValueError(f"Citadel Securities listing total is not numeric: {text!r}")
        return int(text)

    @staticmethod
    def _listing_per_page(soup):
        hidden = soup.select_one(
            "#ajax-careers-search-filter input[name='per_page'][type='hidden']"
        )
        if hidden is None:
            raise ValueError("Citadel Securities listing form has no per_page hidden input")
        try:
            per_page = int((hidden.get("value") or "").strip())
        except ValueError as exc:
            raise ValueError("Citadel Securities listing per_page is not an integer") from exc
        if per_page <= 0:
            raise ValueError("Citadel Securities listing per_page is not positive")
        return per_page

    @staticmethod
    def _listing_form_shape(soup):
        form = soup.select_one("#ajax-careers-search-filter")
        if form is None:
            raise ValueError("Citadel Securities listing filter form is missing")
        action_url = form.get("action") or ""
        if urlsplit(action_url).path != "/wp-admin/admin-ajax.php":
            raise ValueError(f"Citadel Securities listing form action changed: {action_url}")
        hidden_action = form.select_one("input[name='action'][type='hidden']")
        if hidden_action is None or (hidden_action.get("value") or "") != "careers_listing_filter":
            raise ValueError("Citadel Securities listing form no longer uses careers_listing_filter")
        sort_hidden = form.select_one("input[name='sort_order'][type='hidden']")
        if sort_hidden is None or (sort_hidden.get("value") or "") != "DESC":
            raise ValueError("Citadel Securities listing default sort_order changed from DESC")
        return action_url

    @staticmethod
    def _listing_cards(soup, page_url):
        cards = []
        for anchor in soup.select("a.js-career-card[href]"):
            href = urljoin(page_url, anchor.get("href", ""))
            parsed = urlsplit(href)
            if parsed.scheme != "https" or parsed.hostname not in CitadelSecurities.HOSTS:
                raise ValueError(f"Citadel Securities card left the official site: {href}")
            if not DETAIL_PATH.fullmatch(parsed.path or ""):
                raise ValueError(f"Citadel Securities card href is not a native detail route: {href}")
            title = (anchor.get("data-position") or "").strip()
            if not title:
                heading = anchor.select_one(".careers-listing-card__title h2")
                title = heading.get_text(" ", strip=True) if heading else ""
            location_node = anchor.select_one(".careers-listing-card__location")
            location_text = location_node.get_text(" ", strip=True) if location_node else ""
            audience = (anchor.get("data-audience") or "").strip() or None
            cards.append(
                {
                    "url": href,
                    "slug": DETAIL_PATH.fullmatch(parsed.path).group(1),
                    "title": unquote(title).strip(),
                    "listing_location": location_text,
                    "audience": audience,
                    "card_html": str(anchor),
                }
            )
        return cards

    @staticmethod
    def _pagination_pages(soup, page_url, expected_total, per_page):
        linked = set()
        for anchor in soup.select(".career-listing__pagination a.page-numbers[href]"):
            href = urljoin(page_url, anchor.get("href", ""))
            match = PAGE_PATH.fullmatch(urlsplit(href).path or "")
            if match:
                linked.add(int(match.group(1)))
        if not linked:
            raise ValueError("Citadel Securities listing has no pagination links")
        computed = -(-expected_total // per_page)
        # The company pagination truncates middle pages with dots (e.g. page 7
        # is hidden between 6 and 8 on page 1), so the full traversal is the
        # contiguous 1..computed range, cross-checked against the highest link.
        if max(linked) != computed:
            raise ValueError(
                f"Citadel Securities pagination last page {max(linked)} != computed pages {computed} "
                f"(total {expected_total}, per_page {per_page})"
            )
        return list(range(1, computed + 1))

    @staticmethod
    def _parse_detail(detail_url, html):
        soup = BeautifulSoup(html, "lxml")
        title_inner = soup.select_one(".single-job-application__title-inner")
        if title_inner is None or title_inner.find("h1") is None:
            raise ValueError(f"Citadel Securities detail {detail_url} has no hero title block")
        detail_title = title_inner.find("h1").get_text(" ", strip=True)
        hero_location = ""
        for paragraph in title_inner.find_all("p"):
            candidate = paragraph.get_text(" ", strip=True)
            if candidate:
                hero_location = candidate
                break
        text_block = soup.select_one(".single-job-application__description .text")
        if text_block is None:
            raise ValueError(
                f"Citadel Securities detail {detail_url} has no job description block"
            )
        description_html = _description_body_html(text_block)
        full_text_html = str(text_block)
        form = soup.select_one("form.gild-form")
        if form is None:
            raise ValueError(f"Citadel Securities detail {detail_url} has no Gild form")
        hidden = {
            inp.get("name"): (inp.get("value") or "")
            for inp in form.select("input[type='hidden']")
        }
        job_id = (hidden.get("job_id") or "").strip()
        career_post_id = (hidden.get("career_post_id") or "").strip()
        if not UUID.fullmatch(job_id):
            raise ValueError(
                f"Citadel Securities detail {detail_url} has a missing or invalid job_id"
            )
        if not career_post_id.isdigit():
            raise ValueError(
                f"Citadel Securities detail {detail_url} has a missing career_post_id"
            )
        audience = (form.get("data-audience") or "").strip() or None
        description_text = text_html(description_html)
        return {
            "detail_title": detail_title,
            "hero_location": hero_location,
            "description_html": description_html,
            "description_text": description_text,
            "full_text_html": full_text_html,
            "job_id": job_id,
            "career_post_id": career_post_id,
            "audience": audience,
            "salary": _salary_from_description(description_text),
            "attachment_links": _job_attachment_links(description_html),
            "form_action": form.get("action") or "",
            "applying_to": hidden.get("applying_to", ""),
        }

    async def _get_with_retry(self, url, label):
        last_error = None
        for attempt in range(1, self.DETAIL_MAX_ATTEMPTS + 1):
            try:
                return await self.http.get(url)
            except Exception as exc:  # FetchError for HTTP 401/403/429/WAF etc.
                last_error = exc
                message = str(exc)
                retryable = any(
                    code in message for code in ("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 500")
                ) or "FetchError" in type(exc).__name__ or "RequestError" in type(exc).__name__
                if not retryable or attempt >= self.DETAIL_MAX_ATTEMPTS:
                    break
                await asyncio.sleep(min(2 * attempt, 10))
        raise last_error

    async def run(self):
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname not in self.HOSTS
            or entry_parts.path.rstrip("/") != "/careers/open-opportunities"
        ):
            raise ValueError(
                "Citadel Securities dedicated flow must start from its official HTTPS open-opportunities entry"
            )
        listing_started = time.perf_counter()
        entry = await self._get_with_retry(entry_url, "initial listing entry")
        self._assert_company_url(entry.url, prefix="/careers/open-opportunities")
        entry_soup = BeautifulSoup(entry.text, "lxml")
        action_url = self._listing_form_shape(entry_soup)
        expected_total = self._listing_total(entry_soup)
        per_page = self._listing_per_page(entry_soup)
        pages = self._pagination_pages(entry_soup, str(entry.url), expected_total, per_page)
        listing_phase_seconds = time.perf_counter() - listing_started

        initial_cards = self._listing_cards(entry_soup, str(entry.url))
        all_cards = list(initial_cards)
        page_reconciliations = [
            {"page": 1, "url": str(entry.url), "cards": len(initial_cards), "total": expected_total}
        ]
        for page_number in pages:
            if page_number == 1:
                continue
            if not self.page_guard():
                self.result.errors.append("page_budget_exhausted before final listing page")
                break
            page_url = urljoin(str(entry.url), f"page/{page_number}/")
            self._assert_company_url(page_url, prefix="/careers/open-opportunities/page/")
            response = await self._get_with_retry(page_url, f"listing page {page_number}")
            if str(response.url) != page_url:
                raise ValueError(
                    f"Citadel Securities listing page {page_number} redirected to {response.url}"
                )
            page_soup = BeautifulSoup(response.text, "lxml")
            page_total = self._listing_total(page_soup)
            if page_total != expected_total:
                raise ValueError(
                    f"Citadel Securities listing total changed on page {page_number}: "
                    f"{page_total} != {expected_total}"
                )
            cards = self._listing_cards(page_soup, page_url)
            all_cards.extend(cards)
            page_reconciliations.append(
                {"page": page_number, "url": page_url, "cards": len(cards), "total": page_total}
            )
        listing_phase_seconds = time.perf_counter() - listing_started

        urls = [card["url"] for card in all_cards]
        if len(urls) != len(set(urls)):
            raise ValueError("Citadel Securities listing repeats a company-published detail URL")
        if len(urls) != expected_total:
            self.result.errors.append(
                f"citadel_securities_listing_card_count {len(urls)} != published total {expected_total}"
            )

        # Final entry re-check: detect membership change during collection.
        # The re-check time still belongs to the listing phase.
        final_snapshot_verified = True
        final_cards = []
        final_total = expected_total
        try:
            final_entry = await self._get_with_retry(entry_url, "final listing entry")
            final_soup = BeautifulSoup(final_entry.text, "lxml")
            final_total = self._listing_total(final_soup)
            final_cards = self._listing_cards(final_soup, str(final_entry.url))
        except Exception as exc:
            final_snapshot_verified = False
            self.result.errors.append(
                f"Citadel Securities final listing snapshot failed; initial usable cards retained: "
                f"{type(exc).__name__}: {exc}"
            )
        membership_changed = final_snapshot_verified and (
            final_total != expected_total
            or [c["url"] for c in final_cards] != [c["url"] for c in initial_cards]
        )
        if membership_changed:
            self.result.errors.append(
                "citadel_securities_official_listing_membership_changed_between_initial_and_final_snapshots"
            )
        listing_phase_seconds = time.perf_counter() - listing_started

        detail_started = time.perf_counter()
        for card in all_cards:
            if self.limit():
                break
            detail_url = card["url"]
            try:
                response = await self._get_with_retry(detail_url, "detail")
                if str(response.url) != detail_url:
                    raise ValueError(
                        f"Citadel Securities detail {detail_url} redirected to {response.url}"
                    )
                parsed = self._parse_detail(detail_url, response.text)
            except Exception as exc:
                self.result.errors.append(
                    f"Citadel Securities detail {detail_url}: {type(exc).__name__}: {exc}"
                )
                # Retain the listing card so the failure stays visible; the
                # record has no verified description and cannot certify.
                raw = {
                    "listing_card": card,
                    "source_ui": {
                        "listing_api_url": action_url,
                        "listing_page_url": str(entry.url),
                        "apply_surface_url": detail_url,
                        "apply_surface": "gild-form-on-native-detail-page",
                    },
                    "detail_fetch_error": f"{type(exc).__name__}: {exc}",
                }
                self.add(
                    self.job(
                        raw,
                        title=card["title"],
                        url=detail_url,
                        job_id=None,
                        requisition_id=None,
                        apply_url=detail_url,
                        description="",
                        method="citadel_securities.native_detail.job_description_html",
                        description_url=detail_url,
                        locations=_split_locations(card["listing_location"]),
                        employer="Citadel Securities",
                    )
                )
                continue
            if parsed["detail_title"] != card["title"]:
                self.result.errors.append(
                    f"{detail_url}: listing title {card['title']!r} != detail title {parsed['detail_title']!r}"
                )
            if not parsed["description_html"].strip():
                self.result.errors.append(f"{detail_url}: Job Description HTML is empty")
            elif len(parsed["description_text"].strip()) < 100:
                self.result.errors.append(
                    f"{detail_url}: Job Description is implausibly short"
                )
            if parsed["attachment_links"]:
                self.result.errors.append(
                    f"{detail_url}: published role attachment links present but no full attachment text captured"
                )
            raw = {
                "listing_card": card,
                "detail_full_text_html": parsed["full_text_html"],
                "detail_hero": {
                    "title": parsed["detail_title"],
                    "location": parsed["hero_location"],
                },
                "source_ui": {
                    "listing_api_url": action_url,
                    "listing_page_url": str(entry.url),
                    "apply_surface_url": detail_url,
                    "apply_surface": "gild-form-on-native-detail-page",
                    "attachment_links": parsed["attachment_links"],
                    "salary_from_description": parsed["salary"],
                    "audience": parsed["audience"],
                    "career_post_id": parsed["career_post_id"],
                },
            }
            job = self.job(
                raw,
                title=parsed["detail_title"],
                url=detail_url,
                job_id=parsed["job_id"],
                requisition_id=parsed["career_post_id"],
                apply_url=detail_url,
                description=parsed["description_html"],
                method="citadel_securities.native_detail.job_description_html",
                description_url=detail_url,
                locations=_split_locations(parsed["hero_location"]),
                employer="Citadel Securities",
                department=None,
                employment_type=None,
                workplace_type=None,
                date_posted=None,
                date_posted_raw=None,
                date_updated=None,
                salary=parsed["salary"],
            )
            job.description_text = parsed["description_text"]
            job.record_identity = f"citadel-securities:{parsed['job_id']}"
            if membership_changed:
                job.warnings.append("membership_changed_during_collection")
            self.add(job)
        detail_phase_seconds = time.perf_counter() - detail_started

        job_ids = [job.job_id for job in self.result.jobs]
        if len(job_ids) != len(set(job_ids)):
            self.result.errors.append("citadel_securities_detail_job_id_duplicate")
        self.result.expected_total = expected_total
        self.result.pages = len(page_reconciliations)
        if membership_changed or len(urls) != expected_total:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        self.result.metadata.update(
            {
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": str(entry.url),
                "membership_authority": (
                    "Citadel Securities official Open Opportunities entry and its server-rendered "
                    "/page/<n>/ pagination; cards are a.js-career-card hrefs to exact /careers/details/<slug>/ pages"
                ),
                "listing_filter_behavior": (
                    "Unfiltered default (no experience/role/location checkboxes checked). "
                    "Page form #ajax-careers-search-filter POSTs to /wp-admin/admin-ajax.php "
                    "action=careers_listing_filter per_page=10 sort_order=DESC current_page=<n>; "
                    "GET pagination hrefs return the same cards and are collected."
                ),
                "listing_ajax_action_url": action_url,
                "listing_per_page": per_page,
                "listing_pages": page_reconciliations,
                "initial_visible_urls": [c["url"] for c in initial_cards],
                "final_visible_urls": [c["url"] for c in final_cards],
                "final_snapshot_verified": final_snapshot_verified,
                "expected_total": expected_total,
                "observed_total": expected_total if final_snapshot_verified else final_total,
                "description_source": (
                    "Each exact listed detail page's .single-job-application__description .text, "
                    "between Job Description and About Citadel Securities headings"
                ),
                "apply_surface": (
                    "On-page Gild form (form.gild-form, action='', method post) on each native "
                    "detail page; no external ATS URL is published"
                ),
                "detail_job_id_field": "hidden input name=job_id (UUID)",
                "requisition_field": "hidden input name=career_post_id (WordPress post ID)",
                "salary_policy": (
                    "Exact base salary range sentence found in a published Job Description is retained "
                    "with its source text; weekly intern ranges keep the per-week label; "
                    "other compensation remains null."
                ),
                "job_attachment_links": {},
            }
        )
        # Record attachment links actually observed per successful detail.
        self.result.metadata["job_attachment_links"] = {
            job.url: job.raw.get("source_ui", {}).get("attachment_links", [])
            for job in self.result.jobs
            if job.raw.get("source_ui", {}).get("attachment_links")
        }
        self.result.timings["listing_phase_seconds"] = round(listing_phase_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_phase_seconds, 6)
        return self.result
