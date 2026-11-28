"""Da Vinci's own WordPress careers listing and native /job/ detail journey."""
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


SLUG_ROUTE = re.compile(r"^/job/([a-z0-9][a-z0-9-]*)/$")


def _parse_jsonld(html):
    candidates = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    )
    if not candidates:
        # Some pages carry extra attributes (e.g. class="rank-math-schema") on
        # the tag; fall back to a looser match on the type attribute itself.
        candidates = re.findall(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S
        )
    for block in candidates:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("@type") == "JobPosting":
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
    return None


def _is_full_posting_node(posting):
    """RankMath template SEO blocks also carry a JobPosting node, but it is a
    teaser-only description with no Greenhouse identifier and no address.
    Only nodes carrying both are the full published posting metadata."""
    identifier = posting.get("identifier") or {}
    job_location = posting.get("jobLocation") or {}
    address = job_location.get("address") or {}
    return bool(identifier.get("value") and address.get("addressLocality"))


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _postal_location(posting):
    location = posting.get("jobLocation") or {}
    address = location.get("address") or {}
    parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
    parts = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return ", ".join(parts), address


class DaVinci(Adapter):
    """Follow only Da Vinci's official listing cards and exact native job pages."""

    VERIFICATION_REVISION = "davinci.native-careers-list-and-ldjson-details.1"
    CAREERS_HOST = "davincitrading.com"
    BOARD_TOKEN = "davinciderivatives"
    HOSTED_HOST = "job-boards.eu.greenhouse.io"

    @staticmethod
    def _assert_davinci_url(url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != DaVinci.CAREERS_HOST:
            raise ValueError(f"Da Vinci company page asset left the official HTTPS site: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Da Vinci company page asset has an unexpected path: {url}")

    @staticmethod
    def _parse_listing(html):
        soup = BeautifulSoup(html, "lxml")
        grids = soup.select("div.card-grid--careers-jobs")
        if len(grids) != 1:
            raise ValueError(f"Da Vinci entry has {len(grids)} careers job grids (expected 1)")
        cards = []
        seen = set()
        for item in grids[0].select("li.card-grid__item"):
            anchor = item.select_one("a.job-card[href]")
            if anchor is None:
                raise ValueError("Da Vinci listing card has no job-card anchor")
            href = urljoin("https://davincitrading.com/careers/", anchor.get("href", ""))
            parsed = urlsplit(href)
            match = SLUG_ROUTE.fullmatch(parsed.path)
            if (parsed.scheme != "https" or parsed.hostname != DaVinci.CAREERS_HOST
                    or not match or parsed.query or parsed.fragment):
                raise ValueError(f"Da Vinci listing card href is not an exact native job route: {href!r}")
            slug = match.group(1)
            if slug in seen:
                raise ValueError(f"Da Vinci listing repeats a job slug: {slug}")
            seen.add(slug)
            location_el = item.select_one(".job-card__location")
            title_el = item.select_one(".job-card__title")
            cards.append({
                "slug": slug,
                "url": href,
                "card_title": title_el.get_text(" ", strip=True) if title_el else "",
                "card_location": location_el.get_text(" ", strip=True) if location_el else "",
                "data_department": item.get("data-department"),
                "data_office": item.get("data-office"),
            })
        return cards

    @staticmethod
    def _filter_script_url(html):
        soup = BeautifulSoup(html, "lxml")
        candidates = [
            node.get("src")
            for node in soup.select("script[src]")
            if "card-grid.js" in (node.get("src") or "")
        ]
        if len(candidates) != 1:
            raise ValueError(f"Da Vinci entry does not ship exactly one card-grid script (found {len(candidates)})")
        script_url = urljoin("https://davincitrading.com/careers/", candidates[0])
        DaVinci._assert_davinci_url(script_url, path_prefix="/wp-content/themes/davinci/")
        return script_url

    @staticmethod
    def _assert_filter_script(script):
        if "No AJAX" not in script:
            raise ValueError("Da Vinci card-grid script no longer matches the inspected client-side filter flow")

    @staticmethod
    def _parse_detail_page(html, slug):
        posting = _parse_jsonld(html)
        if posting is None:
            raise ValueError(f"Da Vinci /job/{slug}/ has no JobPosting JSON-LD block")
        soup = BeautifulSoup(html, "lxml")
        head = soup.select_one("header.single-event__head")
        title_el = head.select_one("h1.single-event__title") if head else None
        meta_spans = head.select("p.single-event__meta span") if head else []
        department = meta_spans[0].get_text(" ", strip=True) if meta_spans else None
        office = meta_spans[1].get_text(" ", strip=True) if len(meta_spans) > 1 else None
        header_title = title_el.get_text(" ", strip=True) if title_el else ""
        app = soup.select_one("#grnhse_app[data-job-id]")
        greenhouse_id = app.get("data-job-id") if app else None
        identifier = posting.get("identifier") or {}
        identifier_id = identifier.get("value") if isinstance(identifier, dict) else None
        # The fallback iframe URL is the exact hosted apply destination published by the page.
        fallback = re.search(
            r"https://job-boards\.eu\.greenhouse\.io/davinciderivatives/jobs/([0-9]+)",
            html,
        )
        apply_url = (
            f"https://job-boards.eu.greenhouse.io/davinciderivatives/jobs/{fallback.group(1)}"
            if fallback else None
        )
        board_match = re.search(r'"board_token"\s*:\s*"([^"]+)"', html)
        embed_match = re.search(r'boards\.eu\.greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_.-]+)', html)
        return {
            "posting": posting,
            "header_title": header_title,
            "department": department,
            "office": office,
            "greenhouse_id": str(greenhouse_id) if greenhouse_id is not None else None,
            "identifier_id": str(identifier_id) if identifier_id is not None else None,
            "apply_url": apply_url,
            "board_token": board_match.group(1) if board_match else None,
            "embed_board": embed_match.group(1) if embed_match else None,
        }

    @staticmethod
    def _parse_hosted_detail(html, apply_url):
        """Parse the exact hosted job page published by a native page.

        Only ever called for an ID/URL the native Da Vinci page itself exposes.
        The full role body lives in the `div.job__description.body` container.
        """
        soup = BeautifulSoup(html, "lxml")
        container = soup.select_one("div.job__description.body")
        if container is None:
            raise ValueError(f"Hosted job page {apply_url} has no div.job__description.body container")
        return {"container_html": str(container)}

    def _assert_hosted_url(self, url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != self.HOSTED_HOST:
            raise ValueError(f"Hosted fallback left the published Greenhouse EU host: {url}")
        path = f"/{self.BOARD_TOKEN}/jobs/"
        if not parsed.path.startswith(path):
            raise ValueError(f"Hosted fallback is not an exact Da Vinci board job route: {url}")

    def _make_job(self, card, detail, entry_url, *, initial_card=None, membership_changed=False):
        posting = detail["posting"]
        slug = card["slug"]
        url = card["url"]
        full_node = _is_full_posting_node(posting)
        # The native header title is the published product title; the
        # template SEO block appends " - Da Vinci Trading" to its title.
        native_title = posting.get("title") if full_node else posting.get("title", "")
        if isinstance(native_title, str):
            for suffix in (" - Da Vinci Trading", " - Da Vinci"):
                if native_title.endswith(suffix):
                    native_title = native_title[: -len(suffix)]
                    break
        title = detail["header_title"] or native_title or card["card_title"]

        date_posted_raw = posting.get("datePosted") if isinstance(posting.get("datePosted"), str) else None
        if full_node:
            description_html = posting.get("description") or ""
            description_method = "davinci.native_job_page.ldjson.JobPosting.description"
            description_source_url = url
            # Only a full posting node carries a credible published date.
            date_posted = date_posted_raw
            location_text, _address = _postal_location(posting)
        else:
            # Template SEO node: teaser text only, no address. The full body
            # comes from the exact hosted page this native page publishes.
            hosted = detail.get("hosted") or {}
            description_html = hosted.get("container_html") or ""
            description_method = "davinci.greenhouse_hosted_exact_job.job__description"
            description_source_url = detail["apply_url"]
            date_posted = None
            location_text = card["card_location"]
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{slug}: Da Vinci public detail description is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{slug}: Da Vinci public detail description is implausibly short")
        jsonld_title = posting.get("title")
        if (detail["header_title"] and isinstance(jsonld_title, str)
                and jsonld_title != detail["header_title"]
                and jsonld_title != f"{detail['header_title']} - Da Vinci Trading"):
            self.result.errors.append(
                f"{slug}: native header title {detail['header_title']!r} differs from JSON-LD title {jsonld_title!r}"
            )
        if detail["greenhouse_id"] is None:
            self.result.errors.append(f"{slug}: native page has no greenhouse embed job id")
        elif detail["identifier_id"] is not None and detail["identifier_id"] != detail["greenhouse_id"]:
            self.result.errors.append(
                f"{slug}: native greenhouse embed id {detail['greenhouse_id']!r} "
                f"differs from JSON-LD identifier {detail['identifier_id']!r}"
            )
        if detail["board_token"] != self.BOARD_TOKEN or detail["embed_board"] != self.BOARD_TOKEN:
            self.result.errors.append(
                f"{slug}: greenhouse board token {detail['board_token']!r}/{detail['embed_board']!r} "
                f"differs from {self.BOARD_TOKEN!r}"
            )
        expected_apply = (
            f"https://job-boards.eu.greenhouse.io/{self.BOARD_TOKEN}/jobs/{detail['greenhouse_id']}"
            if detail["greenhouse_id"] else None
        )
        if detail["apply_url"] != expected_apply:
            self.result.errors.append(
                f"{slug}: fallback apply URL {detail['apply_url']!r} differs from {expected_apply!r}"
            )
        locations = [{"location": location_text}] if location_text else []
        raw = {
            "listing_card": card,
            "job_posting": posting,
            "detail_header": {
                "title": detail["header_title"],
                "department": detail["department"],
                "office": detail["office"],
            },
            "source_ui": {
                "native_job_url": url,
                "hosted_apply_url": detail["apply_url"],
                "greenhouse_board": self.BOARD_TOKEN,
                "greenhouse_job_id": detail["greenhouse_id"],
                "entry_url": entry_url,
            },
        }
        if not full_node:
            hosted = detail.get("hosted") or {}
            raw["hosted_detail"] = {
                "url": detail["apply_url"],
                "container_html": hosted.get("container_html"),
                "reason": "native JobPosting node is a template SEO block (teaser description, no identifier/address); the exact hosted job page is the published full body",
            }
        if initial_card is not None and initial_card != card:
            raw["initial_listing_card"] = initial_card
        job = self.job(
            raw,
            title=title,
            url=url,
            job_id=detail["greenhouse_id"],
            requisition_id=None,
            apply_url=detail["apply_url"],
            description=description_html,
            method=description_method,
            description_url=description_source_url,
            locations=locations,
            department=detail["department"],
            team=None,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
            date_updated=None,
            salary=None,
        )
        job.description_text = description_text
        job.record_identity = f"davinci:{slug}"
        job.employer = posting.get("hiringOrganization")
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname != DaVinci.CAREERS_HOST
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("Da Vinci dedicated flow must start from its official HTTPS /careers/ entry")

        initial_resp = await self.http.get(entry_url)
        self._assert_davinci_url(initial_resp.url, path_prefix="/careers")
        if str(initial_resp.url) != entry_url:
            raise ValueError("Da Vinci entry request redirected away from its official /careers/ URL")
        initial_cards = self._parse_listing(initial_resp.text)
        filter_script_url = self._filter_script_url(initial_resp.text)
        if not initial_cards:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Da Vinci official listing returned zero job cards; native empty state requires independent review"
            )
            return self.result
        initial_by_slug = {card["slug"]: card for card in initial_cards}
        initial_slugs = [card["slug"] for card in initial_cards]

        # The card-grid filter is pure client-side visibility toggling, so the
        # server-rendered cards are the full unfiltered membership. Fetch the
        # exact shipped script once to pin that behaviour for the audit.
        detail_started = time.perf_counter()
        detail_ui_error = None
        try:
            filter_resp = await self.http.get(filter_script_url)
            self._assert_davinci_url(filter_resp.url, path_prefix="/wp-content/themes/davinci/")
            if str(filter_resp.url) != filter_script_url:
                raise ValueError("Da Vinci card-grid script redirected away from its exact shipped URL")
            self._assert_filter_script(filter_resp.text)
        except Exception as exc:
            detail_ui_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Da Vinci card-grid filter script {filter_script_url}: {detail_ui_error}")
        detail_ui_seconds = time.perf_counter() - detail_started

        mapping_started = time.perf_counter()
        # Every card is a real native page; fetch each exact published route once.
        details = {}
        for card in initial_cards:
            try:
                resp = await self.http.get(card["url"])
                self._assert_davinci_url(resp.url, path_prefix="/job/")
                if str(resp.url) != card["url"]:
                    self.result.errors.append(
                        f"{card['slug']}: native detail route redirected {card['url']} -> {resp.url}"
                    )
                details[card["slug"]] = self._parse_detail_page(resp.text, card["slug"])
            except Exception as exc:
                details[card["slug"]] = None
                self.result.errors.append(
                    f"{card['slug']}: {type(exc).__name__}: {exc}"
                )
        detail_mapping_seconds = time.perf_counter() - mapping_started

        hosted_started = time.perf_counter()
        hosted_fallbacks = []
        for card in initial_cards:
            detail = details.get(card["slug"])
            if detail is None or _is_full_posting_node(detail["posting"]):
                continue
            detail["hosted"] = None
            apply_url = detail["apply_url"]
            try:
                if apply_url is None:
                    raise ValueError("native page publishes no hosted fallback URL")
                self._assert_hosted_url(apply_url)
                hosted_resp = await self.http.get(apply_url)
                self._assert_hosted_url(hosted_resp.url)
                if str(hosted_resp.url) != apply_url:
                    raise ValueError(f"hosted job page redirected {apply_url} -> {hosted_resp.url}")
                detail["hosted"] = self._parse_hosted_detail(hosted_resp.text, apply_url)
                hosted_fallbacks.append(card["slug"])
            except Exception as exc:
                self.result.errors.append(
                    f"{card['slug']}: hosted fallback detail {apply_url}: {type(exc).__name__}: {exc}"
                )
        hosted_fallback_seconds = time.perf_counter() - hosted_started

        final_snapshot_verified = True
        try:
            final_resp = await self.http.get(entry_url)
            self._assert_davinci_url(final_resp.url, path_prefix="/careers")
            if str(final_resp.url) != entry_url:
                raise ValueError("Da Vinci final listing request redirected away from its inspected source")
            final_cards = self._parse_listing(final_resp.text)
        except Exception as exc:
            final_snapshot_verified = False
            final_cards = initial_cards
            self.result.errors.append(
                "Da Vinci final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_slug = {card["slug"]: card for card in final_cards}
        final_slugs = [card["slug"] for card in final_cards]
        changed_membership = final_snapshot_verified and set(initial_slugs) != set(final_slugs)
        if changed_membership:
            self.result.errors.append("davinci_official_listing_membership_changed_between_initial_and_final_snapshots")

        all_slugs = list(dict.fromkeys(final_slugs + initial_slugs))
        changed_cards = [slug for slug in final_by_slug.keys() & initial_by_slug.keys()
                          if final_by_slug[slug] != initial_by_slug[slug]]
        for slug in all_slugs:
            card = final_by_slug.get(slug) or initial_by_slug.get(slug)
            assert card is not None
            detail = details.get(slug)
            if detail is None:
                detail = self._blank_detail(card)
            self.add(self._make_job(
                card, detail, entry_url,
                initial_card=initial_by_slug.get(slug) if initial_by_slug.get(slug) != card else None,
                membership_changed=changed_membership and slug not in final_by_slug,
            ))

        self.result.expected_total = len(final_cards)
        self.result.pages = 1
        if changed_membership:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "membership_authority": "Da Vinci official /careers/ card-grid--careers-jobs list; the server-rendered job cards and their exact /job/<slug>/ routes",
            "listing_filter_behavior": "Departments/Offices chips are pure client-side visibility toggling over all server-rendered cards (card-grid.js: 'No AJAX'); the unfiltered full card set is the membership",
            "ui_pagination": "None for the careers jobs grid; the two sibling grids (careers-teams, careers-benefits) are non-job content",
            "greenhouse_board": self.BOARD_TOKEN,
            "initial_visible_slugs": initial_slugs,
            "final_visible_slugs": final_slugs,
            "final_snapshot_verified": final_snapshot_verified,
            "source_rows_changed_between_snapshots": sorted(changed_cards),
            "filter_script_url": filter_script_url,
            "filter_script_error": detail_ui_error,
            "hosted_fallback_slugs": hosted_fallbacks,
            "description_source": "Full posting nodes: each native /job/<slug>/ page's embedded JobPosting JSON-LD description (full HTML), verified equal to the rendered Greenhouse job__description container for 2 representative roles. Template SEO nodes (no identifier/address, teaser only): the exact hosted job page published by the native page, parsed at div.job__description.body.",
            "salary_policy": "No structured salary field is exposed on either the native or hosted page; prose compensation lines stay inside description_text and salary stays null",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["hosted_fallback_phase_seconds"] = round(hosted_fallback_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    @staticmethod
    def _blank_detail(card):
        return {
            "posting": {},
            "header_title": card["card_title"],
            "department": None,
            "office": None,
            "greenhouse_id": None,
            "identifier_id": None,
            "apply_url": None,
            "board_token": None,
            "embed_board": None,
        }
