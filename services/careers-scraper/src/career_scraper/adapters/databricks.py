"""Databricks' own Gatsby careers listing (page-data) and canonical detail journey.

Reviewed 2026-11-07 against the live official page, reworked 2026-11-08
to advertised-only scope. Membership is the company's own rendered
``job_openings`` listing: one card per department membership, deduplicated
to unique advertised roles. The canonical detail page for each advertised
posting is the server-redirect target of its
``/company/careers/open-positions/job?gh_jid=<gh>`` link, whose Gatsby
page-data ``job.content`` is byte-identical to the listing ``content``.
The Greenhouse embed on the detail page is recorded but never enumerated.
Dataset rows the component groups under no heading (empty-list department
value) are not advertised and are excluded from exports; they are retained
in metadata and the archived page-data responses.
"""
from html import unescape
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import now, text_html
from ..structured import objects

VERIFICATION_REVISION = "databricks.gatsby-page-data-and-canonical-detail-exact.4"

ENTRY_PATH = "/company/careers/open-positions"
JOB_HOSTS = {"databricks.com", "www.databricks.com"}
PAGE_DATA_SUFFIX = "/page-data/company/careers/open-positions/page-data.json"
JOB_DETAIL_CHUNK = "component---src-templates-job-detail-js"
GREENHOUSE_EMBED = "https://boards.greenhouse.io/embed/job_board/js?for=databricks"
PAY_ZONE_PDF = "https://www.databricks.com/sites/default/files/2024-08/us-pay-zone-mapping.pdf"

_REQUISITION = re.compile(r"Req(?:uisition)?\s*ID\s*[:\-]\s*([A-Za-z0-9_-]+)", re.I)
_FIRST_LINE_CODE = re.compile(r"^\(?(?:[A-Z]{2,5}\d+R\d+|P-\d+)\)?$")
_ZONE_RANGE = re.compile(
    r"Zone\s+(?P<zone>\d+)\s+Pay Range\s*(?:\n|\s)*\$?(?P<min>[\d,]+)\s*(?:\n|\s)*—\s*(?:\n|\s)*\$?(?P<max>[\d,]+(?:\s*USD)?)"
)
_FILE_HREF = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)


def slugify(value):
    """Mirror the site's canonical-path slugger (chunks 2177 + redirector proof).

    Lowercase, drop non-ASCII-word punctuation, map each space/underscore to
    a hyphen individually. No collapsing and no stripping: leading/trailing
    spaces and punctuation gaps produce leading/double hyphens (e.g.
    ``account-executive--<gh>`` for a trailing-space title).
    """
    text = str(value or "").lower()
    # The site converts a path slash in a title to a hyphen (e.g. "AI/BI" ->
    # "ai-bi") and drops a non-breaking space, while an ordinary space
    # (including a leading or trailing one) becomes a hyphen.
    text = text.replace("/", "-")
    text = text.replace("\xa0", "")
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    return re.sub(r"[\s_]", "-", text)


def canonical_job_url(row):
    departments = row.get("departments") or [{}]
    dept = departments[0].get("name") if departments and isinstance(departments[0], dict) else ""
    gh = row.get("gh_Id")
    # An empty department name has no path segment of its own; Databricks
    # publishes that detail under the "undefined" slug (see the careers sitemap),
    # so the link resolves instead of producing a malformed double slash.
    dept_slug = "undefined" if not (isinstance(dept, str) and dept.strip()) else slugify(dept)
    return "https://www.databricks.com/company/careers/{}/{}-{}".format(
        dept_slug, slugify(row.get("title") or ""), gh
    )


def compact_body_hash(html):
    """Mirror the browser reference's NFC/whitespace-free body hash."""
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def requisition_id(description_text):
    """Return (requisition, evidence) from an employer-published description.

    Explicit ``Req(uisition) ID: X`` (or ``REQ ID - X``) wins. Otherwise an
    exact first-line bare code (``SLSQ127R32``, ``P-1439``, ``(P-972)``) is
    the company's requisition stamp — the explicit values share the same
    alphabet (``CSQ427R266``, ...). Anything else stays null.
    """
    match = _REQUISITION.search(description_text or "")
    if match:
        return match.group(1), "explicit_req_id"
    lines = [line.strip() for line in (description_text or "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    if first and _FIRST_LINE_CODE.fullmatch(first):
        value = first.strip().strip("()")
        return value, "first_line_code"
    if first.startswith("(") and first.endswith(")"):
        inner = first[1:-1].strip()
        if _FIRST_LINE_CODE.match(inner):
            return inner, "first_line_code"
    return None, None


def salary_zones(description_text):
    """Parse verbatim Zone pay ranges; None when the job publishes none."""
    text = description_text or ""
    zones = []
    for match in _ZONE_RANGE.finditer(text):
        zones.append({
            "zone": f"Zone {match.group('zone')}",
            "min_text": f"${match.group('min')}",
            "max_text": f"${match.group('max')}",
        })
    if not zones:
        return None
    return {
        "zones": zones,
        "source_field": "content",
        "semantics": (
            "Employer-published expected salary range (non-commissionable) or "
            "on-target earnings (commissionable) per pay zone; text retained "
            "verbatim without conversion. Zone mapping is the linked "
            "US pay zone PDF."
        ),
    }


def attachment_links(description_html):
    """All pdf/doc links in a description, employer-hosted flagged."""
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if _FILE_HREF.search(href):
            host = (urlsplit(href).hostname or "").lower()
            values.append({
                "url": href,
                "label": anchor.get_text(" ", strip=True) or None,
                "employer_hosted": host in JOB_HOSTS,
            })
    return values


def filter_dept_groups(rows):
    """Replicate the shipped ``job_openings`` grouping exactly (chunk 2177).

    The component reads ``metadata[1].value`` when the row has more than one
    metadata entry (else ``metadata[0].value``) and then runs::

        let o = value || ["See More Jobs"];
        "string" == typeof o && (o = [o]);
        o.forEach(...)          // one card per member

    So a missing/None/empty-string value falls back to ``See More Jobs``,
    a string is one membership, a list is one membership per member, and an
    empty list ``[]`` is *truthy* in JavaScript: it yields no membership at
    all and the row is therefore not rendered anywhere. Rows are grouped
    from the department member lists, which is exactly what the UI maps over.
    """
    groups = {}
    for row in rows:
        meta = row.get("metadata") or []
        index = 1 if len(meta) > 1 else 0
        value = meta[index].get("value") if index < len(meta) else None
        if isinstance(value, list):
            memberships = value
        elif isinstance(value, str):
            memberships = [value] if value else ["See More Jobs"]
        elif value:
            memberships = [value]
        else:
            memberships = ["See More Jobs"]
        for dept in memberships:
            groups.setdefault(dept, []).append(row)
    return groups


def department_members(department_nodes):
    """Flatten ``allGreenhouseDepartment.nodes[].jobs`` in shipped UI order."""
    return [member for node in (department_nodes or []) for member in (node.get("jobs") or [])]


def _group_order(name):
    """The component's comparator: alphabetical, ``See More Jobs`` last."""
    return (name == "See More Jobs", name)


def _title_order(row):
    return (row.get("title") or "").strip()


def ui_memberships(rows, members=None):
    """Return rendered cards, groups and per-ID group labels for the live UI.

    ``rows`` are the full page-data rows (with bodies); ``members`` are the
    department membership rows the component actually maps over. The returned
    card list keeps duplicate memberships (a dual-labelled role is rendered
    once per group); ``labels`` records those groups per ``gh_Id``.
    """
    members = rows if members is None else members
    rows_by_id = {str(row["gh_Id"]): row for row in rows}
    groups = filter_dept_groups(members)
    cards, labels = [], {}
    for name in sorted(groups, key=_group_order):
        for member in sorted(groups[name], key=_title_order):
            identity = str(member.get("gh_Id"))
            cards.append(rows_by_id.get(identity, member))
            labels.setdefault(identity, []).append(name)
    return cards, groups, labels


def ui_order(rows, members=None):
    """Render order of the cards: groups alphabetical (See More Jobs last), title asc."""
    return ui_memberships(rows, members)[0]


class Databricks(Adapter):
    """Follow only Databricks' own listing page-data and canonical details."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    @staticmethod
    def _page_data_url(entry_url, html):
        soup = BeautifulSoup(html, "lxml")
        prefixes = set()
        for script in soup.select("script[src]"):
            url = urljoin(entry_url, script["src"])
            parsed = urlsplit(url)
            match = re.fullmatch(r"(.+)/app-[^/]+\.js", parsed.path)
            if parsed.hostname == urlsplit(entry_url).hostname and match:
                prefixes.add(f"{parsed.scheme}://{parsed.netloc}{match.group(1)}")
        if len(prefixes) != 1:
            raise ValueError("Databricks careers asset prefix missing or ambiguous")
        return prefixes.pop() + PAGE_DATA_SUFFIX

    def _check_entry_url(self, url):
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname not in JOB_HOSTS
                or parsed.path.rstrip("/") != ENTRY_PATH):
            raise ValueError(f"Databricks flow left its official HTTPS entry: {url}")
        return str(url)

    @staticmethod
    def _component_names(data):
        names = []
        for node in objects(data):
            name = node.get("fieldComponentName")
            if name:
                names.append(name)
        return names

    @staticmethod
    def _rows(payload):
        if not isinstance(payload, dict) or payload.get("path") != ENTRY_PATH:
            raise ValueError("Databricks page data does not identify its job-listing page")
        jobs = [node["allGreenhouseJob"] for node in objects(payload) if "allGreenhouseJob" in node]
        depts = [node["allGreenhouseDepartment"] for node in objects(payload) if "allGreenhouseDepartment" in node]
        if len(jobs) != 1 or not isinstance(jobs[0].get("nodes"), list):
            raise ValueError("Databricks full company jobs dataset missing or ambiguous")
        if len(depts) != 1 or not isinstance(depts[0].get("nodes"), list):
            raise ValueError("Databricks department dataset missing or ambiguous")
        if "job_openings" not in Databricks._component_names(payload):
            raise ValueError("Databricks page data lost its inspected job_openings component")
        rows = jobs[0]["nodes"]
        ids = [str(row.get("gh_Id")) if isinstance(row, dict) else None for row in rows]
        if any(not identity or not identity.isdigit() for identity in ids):
            raise ValueError("Databricks listing contains a missing or invalid gh_Id")
        if len(ids) != len(set(ids)):
            raise ValueError("Databricks listing repeats a company-published ID")
        for row in rows:
            if not all(k in row for k in ("gh_Id", "absolute_url", "content")):
                raise ValueError("Databricks posting schema changed")
            host = urlsplit(row["absolute_url"]).hostname
            if host not in JOB_HOSTS:
                raise ValueError("Databricks posting changed its job-page destination")
            if not row["absolute_url"].startswith(("http://", "https://")):
                raise ValueError("Databricks posting has an invalid absolute_url")
            if not (row.get("title") or "").strip():
                raise ValueError("Databricks listing contains an unidentified job")
            if row.get("content") is None:
                raise ValueError("Databricks posting is missing its content field")
        dept_ids = set()
        for dept in depts[0]["nodes"]:
            for member in dept.get("jobs") or []:
                dept_ids.add(str(member.get("gh_Id")))
        unknown = dept_ids - set(ids)
        if unknown:
            raise ValueError(
                "Databricks department rendering references rows outside the full dataset: "
                + ", ".join(sorted(unknown))
            )
        # A full-dataset row absent from every department member list is not
        # rendered by the component at all; the run records it as a hidden
        # row excluded from advertised exports (see run()) instead of
        # silently dropping or inventing membership.
        return rows, depts[0]["nodes"]

    async def _snapshot(self, api_url, label):
        response = await self.http.get(api_url)
        if str(response.url) != api_url:
            raise ValueError(f"Databricks {label} listing request redirected from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Databricks {label} listing response is not JSON") from exc
        rows, depts = self._rows(payload)
        return payload, rows, depts

    async def _canonical_check(self, listing_rows, members, label="representative"):
        ordered = ui_order(listing_rows, members)
        last_error = None
        for row in ordered[:6]:
            if not self.page_guard():
                return None, None, None, "page budget exhausted"
            gh = str(row["gh_Id"])
            canon = canonical_job_url(row)
            pd_url = canon.replace(
                "https://www.databricks.com/company/careers/",
                "https://www.databricks.com/careers-assets/page-data/company/careers/",
            ) + "/page-data.json"
            try:
                response = await self.http.get(pd_url)
                if str(response.url) != pd_url:
                    raise ValueError("canonical detail page-data redirected")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ValueError("canonical detail page-data is not JSON") from exc
                if payload.get("componentChunkName") != JOB_DETAIL_CHUNK and (
                    not str(payload.get("componentChunkName", "")).startswith(JOB_DETAIL_CHUNK)
                ):
                    raise ValueError("canonical slug resolved to a non-detail page")
                context = payload.get("result", {}).get("pageContext", {}) or {}
                if str(context.get("greenhouseId")) != gh:
                    raise ValueError("canonical detail page-data names a different gh_Id")
                job = context.get("job") or {}
                if job.get("content") != row.get("content"):
                    raise ValueError("canonical detail body differs from listing content")
                if (job.get("title") or "") != (row.get("title") or ""):
                    raise ValueError("canonical detail title differs from listing title")
                if (job.get("location") or {}).get("name") != (row.get("location") or {}).get("name"):
                    raise ValueError("canonical detail location differs from listing location")
                page = await self.http.get(canon)
                if str(page.url) not in {canon, canon + "?gh_jid=" + gh}:
                    raise ValueError("canonical detail page redirected to a different page")
                soup = BeautifulSoup(page.text, "lxml")
                bodies = soup.select("div.rich-text-body")
                if not bodies:
                    raise ValueError("canonical detail page has no rendered rich-text body")
                rendered = bodies[0].get_text(" ", strip=True)
                title = (row.get("title") or "").strip()
                if title and title not in rendered and title not in page.text:
                    raise ValueError("canonical title is absent from its rendered detail page")
                return canon, pd_url, page, None
            except Exception as exc:  # bounded search for a verifiable representative
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        return None, None, None, last_error or "no canonical candidate verified"

    def _make_job(self, row, *, api_url, canon_url, canon_pd_url, initial_row=None,
                  membership_changed=False, rendered_groups=None, hidden=False,
                  detail_link_failure=None):
        identity = str(row["gh_Id"])
        description_html = unescape(row.get("content") or "")
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Databricks public detail content is empty")
        req, req_evidence = requisition_id(description_text)
        locations = [row["location"]] if row.get("location") else []
        locations += row.get("offices") or []
        locations = list({json.dumps(loc, sort_keys=True): loc for loc in locations}.values())
        meta = row.get("metadata") or []
        employer = meta[0].get("value") if meta else None
        if not isinstance(employer, str) or not employer.strip():
            employer = None
        attachments = attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "canonical_url": canon_url,
                "canonical_page_data_url": canon_pd_url,
                "listing_page_data_url": api_url,
                "requisition_evidence": req_evidence,
                "salary": salary_zones(description_text),
                "attachment_links": attachments,
                "employer": employer,
                "rendered_in_listing": not hidden,
                "rendered_groups": rendered_groups or [],
                "hidden_from_published_listing": hidden,
            },
        }
        if detail_link_failure is not None:
            raw["source_ui"]["detail_link_failure"] = detail_link_failure
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=unescape(row.get("title") or ""),
            url=row["absolute_url"],
            job_id=identity,
            requisition_id=req,
            apply_url=None,
            description=description_html,
            method="databricks.page_data.allGreenhouseJob.content",
            description_url=api_url,
            locations=locations,
            department=row.get("departments"),
            employer=employer,
            date_posted=None,
            date_posted_raw=None,
            date_updated=row.get("updated_at"),
            salary=salary_zones(description_text),
        )
        job.description_text = description_text
        job.record_identity = f"databricks:{identity}"
        if hidden:
            job.warnings.append("hidden_from_published_listing")
        if detail_link_failure is not None:
            job.warnings.append("detail_link_broken")
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def _broken_detail_links(self, rows):
        """Probe rows whose published canonical slug cannot be well formed.

        Historically an empty ``departments[0].name`` slugified to
        ``/company/careers//<title>-<gh>`` (HTTP 404). Databricks now
        publishes that case under the ``undefined`` slug (careers sitemap),
        so the live probe is empty. The check is retained so a future
        malformed slug is still recorded with its real HTTP status instead
        of being hidden.
        """
        failures = []
        for row in rows:
            canon = canonical_job_url(row)
            if "/careers//" not in canon:
                continue
            identity = str(row["gh_Id"])
            failure = {
                "id": identity,
                "title": unescape(row.get("title") or ""),
                "published_url": row["absolute_url"],
                "canonical_url": canon,
                "status": None,
                "final_url": None,
                "reason": None,
                "checked_at": now(),
            }
            try:
                response = await self.http.get(row["absolute_url"])
                failure["status"] = response.status_code
                failure["final_url"] = str(response.url)
                failure["reason"] = "published detail link resolved but its canonical slug is malformed"
            except FetchError as exc:
                message = str(exc)
                matched = re.match(r"HTTP (\d{3})", message)
                failure["status"] = int(matched.group(1)) if matched else None
                failure["final_url"] = canon
                failure["reason"] = message
            except Exception as exc:  # network errors are recorded, never hidden
                failure["reason"] = f"{type(exc).__name__}: {exc}"
            failures.append(failure)
        return failures

    async def run(self):
        started = self.result._started_clock
        entry_url = self._check_entry_url(self.company.careers_url)
        if not self.page_guard():
            return self.result

        entry = await self.http.get(entry_url)
        self._check_entry_url(entry.url)
        api_url = self._page_data_url(str(entry.url), entry.text)
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or parsed.hostname not in JOB_HOSTS \
                or not str(api_url).endswith(PAGE_DATA_SUFFIX):
            raise ValueError(f"Databricks listing source left the official site: {api_url}")

        initial_payload, initial_rows, initial_depts = await self._snapshot(api_url, "initial")
        if not initial_rows:
            self.result.expected_total = 0
            self.result.errors.append(
                "Databricks official page-data returned zero jobs; native empty state requires independent review"
            )
            return self.result
        if not initial_depts:
            self.result.expected_total = 0
            self.result.errors.append(
                "Databricks published listing has no department sections; the component renders zero cards"
            )
            return self.result
        initial_members = department_members(initial_depts)

        detail_started = time.perf_counter()
        canon_url, canon_pd_url, _canon_page, detail_error = await self._canonical_check(
            initial_rows, initial_members)
        if detail_error is not None:
            self.result.errors.append(
                "Databricks representative canonical detail could not be verified: "
                + str(detail_error)
            )
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        final_members = initial_members
        try:
            _final_payload, final_rows, final_depts = await self._snapshot(api_url, "final")
            final_members = department_members(final_depts)
        except Exception as exc:
            final_snapshot_verified = False
            _final_payload, final_rows, final_depts = initial_payload, initial_rows, initial_depts
            self.result.errors.append(
                "Databricks final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )

        initial_by_id = {str(r["gh_Id"]): r for r in initial_rows}
        final_by_id = {str(r["gh_Id"]): r for r in final_rows}
        changed = final_snapshot_verified and set(initial_by_id) != set(final_by_id)
        if changed:
            self.result.errors.append("databricks_official_listing_membership_changed_between_initial_and_final_snapshots")
        changed_rows = sorted(i for i in final_by_id.keys() & initial_by_id.keys()
                              if final_by_id[i] != initial_by_id[i])

        initial_cards, initial_groups, initial_labels = ui_memberships(initial_rows, initial_members)
        final_cards, groups, final_labels = ui_memberships(final_rows, final_members)
        initial_card_ids = [str(r["gh_Id"]) for r in initial_cards]
        final_card_ids = [str(r["gh_Id"]) for r in final_cards]
        rendered_changed = final_snapshot_verified and initial_card_ids != final_card_ids
        if rendered_changed:
            self.result.errors.append(
                "databricks_rendered_listing_cards_changed_between_initial_and_final_snapshots"
            )
        rendered_unique = set(final_card_ids)
        hidden_final = [identity for identity in final_by_id if identity not in rendered_unique]
        member_ids = {str(member.get("gh_Id")) for member in final_members}
        hidden_reasons = {}
        for identity in hidden_final:
            meta = final_by_id[identity].get("metadata") or []
            index = 1 if len(meta) > 1 else 0
            value = meta[index].get("value") if index < len(meta) else None
            if isinstance(value, list) and not value:
                hidden_reasons[identity] = "metadata_department_value_is_empty_list_renders_no_group"
            elif identity not in member_ids:
                hidden_reasons[identity] = "absent_from_all_department_member_lists"
            else:
                hidden_reasons[identity] = "no_rendered_department_group"

        # Advertised scope is the rendered listing only. Dataset rows the
        # component groups under no heading (empty-list department value)
        # are excluded from jobs.jsonl/CSV/SQLite and expected_total so
        # advertised_jobs == jobs_collected == descriptions_verified when
        # the listing is complete. They remain in metadata + archived
        # page-data responses (option (a) of the parent rework).
        advertised_rows = [final_by_id[i] for i in dict.fromkeys(final_card_ids)]
        employer_pdfs, external_refs = await self._employer_attachments(advertised_rows)

        detail_started = time.perf_counter()
        failed_detail_links = await self._broken_detail_links(advertised_rows)
        failure_by_id = {item["id"]: item for item in failed_detail_links}
        detail_link_seconds = time.perf_counter() - detail_started

        advertised_ids = list(dict.fromkeys(final_card_ids))
        mapping_started = time.perf_counter()
        for identity in advertised_ids:
            row = final_by_id[identity]
            canon = canonical_job_url(row)
            pd_url = canon.replace(
                "https://www.databricks.com/company/careers/",
                "https://www.databricks.com/careers-assets/page-data/company/careers/",
            ) + "/page-data.json"
            labels = final_labels.get(identity)
            self.add(self._make_job(
                row, api_url=api_url, canon_url=canon, canon_pd_url=pd_url,
                initial_row=initial_by_id.get(identity) if initial_by_id.get(identity) != row else None,
                membership_changed=False,
                rendered_groups=labels,
                hidden=False,
                detail_link_failure=failure_by_id.get(identity),
            ))
            if self.limit():
                break
        detail_mapping_seconds = time.perf_counter() - mapping_started

        hidden_from_listing_jobs = [
            {
                "id": identity,
                "title": unescape(final_by_id[identity].get("title") or ""),
                "departments": final_by_id[identity].get("departments"),
                "absolute_url": final_by_id[identity].get("absolute_url"),
                "canonical_url": canonical_job_url(final_by_id[identity]),
                "reason": hidden_reasons[identity],
            }
            for identity in hidden_final
        ]

        self.result.expected_total = len(rendered_unique)
        self.result.pages = 1
        if changed or rendered_changed:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        duplicate_memberships = len(final_card_ids) - len(rendered_unique)
        membership_equation = (
            f"{len(final_rows)} dataset rows + {duplicate_memberships} duplicate "
            f"department-membership labels - {len(hidden_final)} rows the component "
            f"groups under no heading = {len(final_card_ids)} rendered cards; "
            f"{len(rendered_unique)} unique advertised roles"
        )
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_field": "allGreenhouseJob.nodes",
            "page_data_url": api_url,
            "canonical_detail_page_opened": canon_url,
            "canonical_page_data_url": canon_pd_url,
            "canonical_detail_verified": detail_error is None,
            "greenhouse_embed": GREENHOUSE_EMBED,
            "membership_authority": (
                "Databricks job_openings component renders one card per "
                "allGreenhouseDepartment.nodes[].jobs member, grouped by "
                "metadata[1].value (metadata[0].value when single); empty-list values "
                "render no card. Advertised membership is the deduplicated rendered "
                "cards. Full bodies come from allGreenhouseJob.nodes."
            ),
            "listing_filter_behavior": (
                "job_openings renders every filtered item without pagination; location, "
                "department and search controls are client-side filters over the "
                f"{len(final_rows)}-row page-data dataset ({len(rendered_unique)} advertised "
                f"rendered roles collected; {len(hidden_final)} dataset rows rendered "
                "under no heading are excluded from exports and retained separately)"
            ),
            "ui_pagination": "None; the component maps all filtered items to cards (no slice/perPage)",
            "filter_dept_groups": {k: len(v) for k, v in sorted(groups.items())},
            "membership": {
                "dataset_rows": len(final_rows),
                "department_member_rows": len(final_members),
                "sections_rendered": len(groups),
                "rendered_cards": len(final_card_ids),
                "rendered_unique_roles": len(rendered_unique),
                "duplicate_memberships": duplicate_memberships,
                "hidden_from_listing_ids": hidden_final,
                "hidden_from_listing_reasons": hidden_reasons,
                "hidden_from_listing_jobs": hidden_from_listing_jobs,
                "excluded_from_exports_ids": hidden_final,
                "advertised_jobs": len(rendered_unique),
                "equation": membership_equation,
            },
            "advertised_jobs": len(rendered_unique),
            "advertised_jobs_basis": (
                "unique roles rendered by the official listing: "
                f"{len(final_card_ids)} cards, {duplicate_memberships} dual-labelled duplicates, "
                f"{len(hidden_final)} page-data rows rendered under no heading are excluded "
                "from jobs.jsonl/CSV/SQLite and expected_total; retained in metadata "
                "(hidden_from_listing_jobs) and archived page-data responses"
            ),
            "hidden_from_listing_jobs": hidden_from_listing_jobs,
            "failed_detail_links": failed_detail_links,
            "initial_nodes_ids": [str(r["gh_Id"]) for r in initial_rows],
            "final_nodes_ids": [str(r["gh_Id"]) for r in final_rows],
            "initial_ui_ids": initial_card_ids,
            "final_ui_ids": final_card_ids,
            "final_snapshot_verified": final_snapshot_verified,
            "source_rows_changed_between_snapshots": changed_rows,
            "employer_attachments": employer_pdfs,
            "external_reference_link_count": external_refs,
            "description_source": "allGreenhouseJob.nodes[].content (escaped HTML), byte-identical to canonical pageContext.job.content on the representative detail page",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["detail_link_probe_seconds"] = round(detail_link_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    async def _employer_attachments(self, rows):
        employer_urls = sorted({item["url"] for row in rows
                                for item in attachment_links(unescape(row.get("content") or ""))
                                if item["employer_hosted"]})
        external = sum(len([item for item in attachment_links(unescape(row.get("content") or ""))
                            if not item["employer_hosted"]]) for row in rows)
        captured = {}
        for url in employer_urls[:5]:
            response = await self.http.get(url)
            body = response.content
            text = ""
            extractable = False
            try:
                from pypdf import PdfReader
                from io import BytesIO
                reader = PdfReader(BytesIO(body))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
                extractable = True
            except Exception:
                text = ""
            captured[url] = {
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "text_chars": len(text),
                "text_available": extractable and bool(text.strip()),
                "text": text if extractable else "",
                "linked_job_count": sum(
                    1 for row in rows
                    if url in [item["url"] for item in attachment_links(unescape(row.get("content") or ""))]
                ),
            }
        if len(employer_urls) > 5:
            self.result.errors.append(
                f"Databricks employer-hosted attachments exceed fetch budget ({len(employer_urls)} distinct; 5 captured)"
            )
        return captured, external
