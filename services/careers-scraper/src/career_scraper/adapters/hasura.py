"""Hasura's official careers journey: hasura.io/careers -> PromptQL careers -> the PromptQL Gem board."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://hasura.io/careers/"
FINAL_CAREERS_URL = "https://promptql.io/careers"
LISTING_URL = "https://jobs.gem.com/promptql"
BOARD_ID = "promptql"
API_URL = "https://jobs.gem.com/api/public/graphql/batch"
EXPECTED_TEAM = "PromptQL"
EXPECTED_PAGE_TITLE = "PromptQL Careers"
DETAIL_URL_PREFIX = "https://jobs.gem.com/promptql/"

BOARD_QUERY = """query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations {
        id
        name
        city
        isoCountry
        isRemote
        extId
        __typename
      }
      job {
        id
        department {
          id
          name
          extId
          __typename
        }
        locationType
        employmentType
        __typename
      }
      __typename
    }
    __typename
  }
  oatsExternalJobPostingsFilters(boardId: $boardId) {
    type
    displayName
    rawValue
    value
    count
    __typename
  }
  jobBoardExternal(vanityUrlPath: $boardId) {
    id
    teamDisplayName
    descriptionHtml
    pageTitle
    __typename
  }
}
"""

THEME_QUERY = """query JobBoardTheme($boardId: String!) {
  publicBrandingTheme(externalId: $boardId) {
    id
    theme
    __typename
  }
}
"""

DETAIL_QUERY = """fragment PublicQuestionFragment on PublicOatsQuestion {
  extId
  answerType
  displayType
  fileType
  text
  description
  isRequired
  options {
    extId
    value
    __typename
  }
  numericRatingRange
  __typename
}

fragment PublicDemographicQuestionFragment on PublicOatsDemographicSurveyQuestion {
  extId
  answerType
  displayType
  text
  description
  options {
    extId
    value
    __typename
  }
  __typename
}

query ExternalJobPosting($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    extId
    startDateTs
    firstPublishedTsSec
    companyLogo
    companyUrl
    isApplicationFormHidden
    applicationFormTemplate {
      id
      includeEeoc
      eeocConfig {
        includeRaceXGender
        includeVeteranStatus
        includeDisabilityStatus
        __typename
      }
      __typename
    }
    isUnlistedExternally
    locations {
      id
      extId
      name
      city
      isoCountry
      isRemote
      __typename
    }
    job {
      id
      locationType
      employmentType
      requisitionId
      teamDisplayName
      department {
        id
        extId
        name
        __typename
      }
      locations {
        id
        extId
        name
        city
        isoCountry
        isRemote
        __typename
      }
      __typename
    }
    jobPostSectionHtml {
      introHtml
      outroHtml
      __typename
    }
    compensationHtml
    __typename
  }
  oatsJobPostFieldsAndQuestions(
    jobBoardVanityPath: $boardId
    jobPostExtId: $extId
  ) {
    fields {
      fieldType
      isRequired
      __typename
    }
    questions {
      ...PublicQuestionFragment
      __typename
    }
    demographicSurvey {
      extId
      surveyType
      questions {
        ...PublicDemographicQuestionFragment
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

BOARD_OPERATIONS = [
    {"operationName": "JobBoardList", "variables": {"boardId": BOARD_ID}, "query": BOARD_QUERY},
    {"operationName": "JobBoardTheme", "variables": {"boardId": BOARD_ID}, "query": THEME_QUERY},
]

EMPLOYMENT_TYPES = {
    "FULL_TIME": "Full-time",
    "PART_TIME": "Part-time",
    "CONTRACT": "Contract",
    "TEMPORARY": "Temporary",
    "INTERN": "Internship",
    "OTHER": "Other",
}
WORKPLACE_TYPES = {
    "REMOTE": "Remote",
    "HYBRID": "Hybrid",
    "ONSITE": "On-site",
    "ON_SITE": "On-site",
}
CURRENCIES = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}
SALARY_RANGE = re.compile(
    r"(?P<cur>[$₹€£])?\s*(?P<min>[\d][\d,]*)\s*(?:-|–|—|to)\s*"
    r"(?P<cur2>[$₹€£])?\s*(?P<max>[\d][\d,]*)"
)
TALENT_COMMUNITY = re.compile(r"\btalent community\b", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

EMPLOYER = {
    "name": "Hasura",
    "doing_business_as": "PromptQL",
    "source": "Official careers journey: hasura.io/careers redirects to promptql.io/careers; "
              "job descriptions state 'While our official company name is Hasura, we do business as PromptQL.'",
}


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_title(value: str | None) -> str:
    return normalized_space(value).strip()


def detail_url(ext_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ext_id or ""):
        raise ValueError(f"Gem posting extId is not a safe public path segment: {ext_id!r}")
    return DETAIL_URL_PREFIX + quote(ext_id, safe="")


def posted_date(timestamp: Any) -> str | None:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_official_entry(html: str, response_url: str, history: list[dict] | None = None) -> dict:
    """Validate the redirect chain and the exact PromptQL -> Gem listing link."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "promptql.io" or parsed.path.rstrip("/") != "/careers":
        raise ValueError("Hasura official careers entry no longer redirects to https://promptql.io/careers")
    redirects = [hop for hop in (history or []) if hop.get("location")]
    if not redirects:
        raise ValueError("Hasura official careers entry no longer has the observed redirect to PromptQL")
    if not any(urlsplit(str(hop.get("location", ""))).netloc == "promptql.io" for hop in redirects):
        raise ValueError("Hasura official careers redirect no longer targets promptql.io")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    canonical_node = soup.select_one('link[rel="canonical"]')
    canonical = canonical_node.get("href") if canonical_node else None
    if canonical and canonical.rstrip("/") != FINAL_CAREERS_URL:
        raise ValueError(f"PromptQL careers canonical link changed: {canonical}")
    anchors = [anchor for anchor in soup.select("a[href]")
               if anchor.get("href") == LISTING_URL]
    if not anchors:
        raise ValueError("PromptQL careers page no longer links to the observed Gem board")
    labels = [normalized_title(anchor.get_text(" ", strip=True)) for anchor in anchors]
    if not any(re.search(r"\bopen\b", label, re.I) and re.search(r"\b(role|position)s?\b", label, re.I)
               for label in labels):
        raise ValueError("PromptQL careers page no longer exposes an open-roles link to the Gem board")
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "status": 200,
        "title": title,
        "canonical": canonical,
        "listing_url": LISTING_URL,
        "listing_link_labels": labels,
        "redirects": redirects,
    }


def _graphql_entries(value: Any) -> list[dict]:
    entries = value if isinstance(value, list) else [value]
    return [entry for entry in entries if isinstance(entry, dict)]


def parse_board_response(value: Any) -> dict:
    """Parse the exact unfiltered JobBoardList/JobBoardTheme batch response."""
    listing = None
    theme = None
    for entry in _graphql_entries(value):
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("oatsExternalJobPostings"), dict):
            listing = data
        if isinstance(data.get("publicBrandingTheme"), dict):
            theme = data["publicBrandingTheme"]
    if listing is None:
        errors = [entry.get("errors") for entry in _graphql_entries(value) if entry.get("errors")]
        raise ValueError(f"Gem board listing response is missing oatsExternalJobPostings: {errors}")
    board = listing.get("jobBoardExternal")
    if not isinstance(board, dict):
        raise ValueError("Gem board response is missing jobBoardExternal identity")
    if board.get("teamDisplayName") != EXPECTED_TEAM:
        raise ValueError(f"Gem board teamDisplayName changed: {board.get('teamDisplayName')!r}")
    if board.get("pageTitle") != EXPECTED_PAGE_TITLE:
        raise ValueError(f"Gem board pageTitle changed: {board.get('pageTitle')!r}")
    filters = listing.get("oatsExternalJobPostingsFilters")
    if not isinstance(filters, list):
        raise ValueError("Gem board response is missing its public filter options")
    posts = (listing.get("oatsExternalJobPostings") or {}).get("jobPostings")
    if not isinstance(posts, list) or not posts:
        raise ValueError("Gem board exposes no public postings; refusing to report an empty membership")
    rows = []
    seen = set()
    for row in posts:
        if not isinstance(row, dict):
            raise ValueError("Gem board posting row is not an object")
        ext_id = str(row.get("extId") or "").strip()
        title = normalized_title(row.get("title"))
        if not ext_id or not title:
            raise ValueError("Gem board posting is missing its extId/title")
        if ext_id in seen:
            raise ValueError(f"Gem board contains a duplicate posting extId: {ext_id}")
        job = row.get("job")
        department = (job or {}).get("department") if isinstance(job, dict) else None
        if not isinstance(job, dict) or not isinstance(department, dict) or not normalized_title(department.get("name")):
            raise ValueError(f"Gem board posting is missing its job/department identity: {ext_id}")
        if not isinstance(row.get("locations"), list):
            raise ValueError(f"Gem board posting is missing its locations list: {ext_id}")
        if not isinstance(job.get("employmentType"), str) or not isinstance(job.get("locationType"), str):
            raise ValueError(f"Gem board posting is missing its employment/workplace type: {ext_id}")
        seen.add(ext_id)
        rows.append(row)
    return {"rows": rows, "filters": filters, "board": board, "theme": theme, "count": len(rows)}


def parse_detail_response(value: Any, ext_id: str, listing_row: dict) -> dict:
    """Extract and validate one exact ExternalJobPosting response."""
    detail = None
    for entry in _graphql_entries(value):
        if entry.get("errors"):
            raise ValueError(f"Gem detail GraphQL error for {ext_id}: {entry['errors']}")
        data = entry.get("data")
        if isinstance(data, dict) and isinstance(data.get("oatsExternalJobPosting"), dict):
            detail = data["oatsExternalJobPosting"]
    if detail is None:
        raise ValueError(f"Gem detail response is missing oatsExternalJobPosting for {ext_id}")
    if detail.get("extId") != ext_id:
        raise ValueError(f"Gem detail response extId mismatch: {detail.get('extId')!r} != {ext_id!r}")
    if normalized_title(detail.get("title")) != normalized_title(listing_row.get("title")):
        raise ValueError(f"Gem detail title changed after the listing: {detail.get('title')!r}")
    job = detail.get("job")
    if not isinstance(job, dict):
        raise ValueError(f"Gem detail is missing job fields: {ext_id}")
    if job.get("teamDisplayName") != EXPECTED_TEAM:
        raise ValueError(f"Gem detail teamDisplayName changed: {job.get('teamDisplayName')!r}")
    if detail.get("isUnlistedExternally") is not False:
        raise ValueError(f"Gem detail is unlisted externally: {ext_id}")
    if not isinstance(detail.get("descriptionHtml"), str):
        raise ValueError(f"Gem detail is missing descriptionHtml: {ext_id}")
    if not isinstance(detail.get("locations"), list):
        raise ValueError(f"Gem detail is missing locations: {ext_id}")
    if not isinstance(job.get("requisitionId"), str) or not job.get("requisitionId").strip():
        detail = dict(detail)
        detail.setdefault("_warnings", []).append("requisition_id_missing")
    return detail


def _is_heading(node: Any) -> bool:
    if not isinstance(node, Tag):
        return False
    if node.name in HEADING_TAGS:
        return bool(normalized_title(node.get_text(" ", strip=True)))
    if node.name not in {"div", "p"}:
        return False
    strong = node.find(["strong", "b"])
    if strong is None:
        return False
    text = normalized_title(node.get_text(" ", strip=True))
    strong_text = normalized_title(strong.get_text(" ", strip=True))
    if not text or text != strong_text:
        return False
    if len(text) > 70 or len(text.split()) > 12:
        return False
    # Bold full sentences in the Talent Community post are paragraphs, not headings.
    if text.endswith((".", "?", "!")) and not text.endswith("..."):
        return False
    return True


def description_sections(html: str) -> list[dict]:
    """Split the published description into its heading/list sections without inventing text."""
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.body or soup
    raw_sections: list[dict] = []
    current = {"heading": None, "blocks": []}
    for child in list(root.children):
        if not isinstance(child, Tag):
            continue
        if _is_heading(child):
            if current["heading"] or current["blocks"]:
                raw_sections.append(current)
            current = {"heading": normalized_title(child.get_text(" ", strip=True)), "blocks": []}
        else:
            current["blocks"].append(child)
    if current["heading"] or current["blocks"]:
        raw_sections.append(current)
    sections = []
    for section in raw_sections:
        items: list[str] = []
        texts: list[str] = []
        for block in section["blocks"]:
            list_items = block.find_all("li") if isinstance(block, Tag) else []
            if list_items:
                items.extend(normalized_title(item.get_text(" ", strip=True))
                             for item in list_items if normalized_title(item.get_text(" ", strip=True)))
            else:
                text = normalized_title(block.get_text(" ", strip=True)) if isinstance(block, Tag) else ""
                if text:
                    texts.append(text)
        text = normalized_space(" ".join(texts))
        if section["heading"] or items or text:
            sections.append({"heading": section["heading"], "items": items, "text": text})
    return sections


def select_qualifications(sections: list[dict]) -> Any:
    for section in sections:
        heading = section.get("heading") or ""
        if re.search(r"\b(role requirements|requirements|qualifications|"
                     r"what (?:we|you)(?:'|’)?re looking for|you have)\b", heading, re.I):
            if section.get("items"):
                return section["items"]
            if section.get("text"):
                return section["text"]
    return None


def select_responsibilities(sections: list[dict]) -> Any:
    role_sections = [section for section in sections
                     if section.get("heading") and re.search(
                         r"\b(about the role|the role|what you(?:'|’)ll do|responsibilities|duties)\b",
                         section["heading"], re.I)]
    if not role_sections:
        role_sections = [section for section in sections
                         if section.get("heading") and re.search(r"\babout the team\b",
                                                                 section["heading"], re.I)]
    if not role_sections:
        return None
    return {
        "source": "gem.ExternalJobPosting.descriptionHtml",
        "sections": [
            {"heading": section.get("heading"), "text": section.get("text"),
             "items": section.get("items") or []}
            for section in role_sections
        ],
    }


def _salary_candidates(detail: dict, sections: list[dict]) -> list[tuple[str, str, str | None]]:
    candidates: list[tuple[str, str, str | None]] = []
    compensation_html = detail.get("compensationHtml")
    if isinstance(compensation_html, str) and compensation_html.strip():
        text = normalized_space(text_html(compensation_html))
        if text:
            candidates.append(("gem.compensationHtml", text, compensation_html))
    for section in sections:
        if section.get("heading") and "compensation" in section["heading"].lower():
            parts = [section.get("text") or ""] + list(section.get("items") or [])
            text = normalized_space(" ".join(part for part in parts if part))
            if text:
                candidates.append(("gem.descriptionHtml:Compensation", text, None))
    return candidates


def parse_salary(detail: dict, sections: list[dict]) -> dict | None:
    for source, text, compensation_html in _salary_candidates(detail, sections):
        match = SALARY_RANGE.search(text)
        if not match:
            continue
        symbol = match.group("cur") or match.group("cur2")
        if not symbol:
            if "INR" in text:
                symbol = "₹"
            elif "USD" in text:
                symbol = "$"
        notes = []
        for section in sections:
            if section.get("heading") and "compensation" in section["heading"].lower():
                for line in [section.get("text") or ""] + list(section.get("items") or []):
                    line = normalized_space(line)
                    if line and not SALARY_RANGE.search(line) and line not in notes:
                        notes.append(line)
        period = "year" if re.search(r"per\s+year|per\s+annum|annually|/\s*year|a\s+year", text, re.I) else None
        return {
            "source": source,
            "raw_text": text,
            "currency": CURRENCIES.get(symbol, symbol),
            "min": int(match.group("min").replace(",", "")),
            "max": int(match.group("max").replace(",", "")),
            "period": period,
            "additional_terms": notes,
            "compensation_html": compensation_html,
        }
    return None


def clean_locations(value: list[Any]) -> list[dict]:
    locations = []
    for location in value:
        if not isinstance(location, dict):
            continue
        locations.append({
            "id": location.get("id"),
            "extId": location.get("extId"),
            "name": location.get("name"),
            "city": location.get("city"),
            "isoCountry": location.get("isoCountry"),
            "isRemote": location.get("isRemote"),
        })
    return locations


def compose_description(detail: dict) -> str:
    sections = detail.get("jobPostSectionHtml") or {}
    parts = [
        sections.get("introHtml") if isinstance(sections, dict) else None,
        detail.get("descriptionHtml"),
        sections.get("outroHtml") if isinstance(sections, dict) else None,
        detail.get("compensationHtml"),
    ]
    return "\n".join(part for part in parts if isinstance(part, str) and part.strip())


def detail_record(detail: dict, listing_row: dict) -> dict:
    description_html = compose_description(detail)
    main_html = detail.get("descriptionHtml") or ""
    sections = description_sections(main_html)
    title = normalized_title(detail.get("title"))
    job = detail.get("job") or {}
    department = (job.get("department") or {}).get("name") if isinstance(job.get("department"), dict) else None
    employment = EMPLOYMENT_TYPES.get(job.get("employmentType"), job.get("employmentType"))
    workplace = WORKPLACE_TYPES.get(job.get("locationType"), job.get("locationType"))
    talent = bool(TALENT_COMMUNITY.search(title) or TALENT_COMMUNITY.search(text_html(main_html)))
    warnings = list(detail.get("_warnings") or [])
    if talent:
        warnings.append("talent_community_pipeline")
    timestamp = detail.get("firstPublishedTsSec")
    return {
        "title": title,
        "description_html": description_html,
        "description_text": text_html(description_html),
        "requisition_id": (job.get("requisitionId") or None),
        "date_posted": posted_date(timestamp),
        "date_posted_raw": timestamp,
        "salary": parse_salary(detail, sections),
        "qualifications": select_qualifications(sections),
        "responsibilities": select_responsibilities(sections),
        "locations": clean_locations(detail.get("locations") or []),
        "department": department,
        "team": job.get("teamDisplayName"),
        "employment_type": employment,
        "workplace_type": workplace,
        "posting_kind": "talent_community" if talent else "job",
        "warnings": warnings,
        "sections": sections,
        "employer": EMPLOYER,
        "employer_statement_present": "official company name is hasura" in text_html(main_html).lower(),
    }


class Hasura(Adapter):
    """Company-specific PromptQL/Gem board flow published from Hasura's own careers URL."""

    async def _post(self, operations: list[dict]) -> tuple[Any, str]:
        response = await self.http.request(
            "POST", API_URL, headers={"batch": "true"}, json=operations
        )
        try:
            value = response.json()
        except ValueError as exc:
            raise ValueError(f"Gem GraphQL response was not JSON: {exc}") from exc
        return value, sha256(response.content).hexdigest()

    async def _verify_official_entry(self) -> dict:
        response = await self.http.get(OFFICIAL_ENTRY)
        history = [
            {"status": hop.status_code, "url": str(hop.request.url),
             "location": hop.headers.get("location")}
            for hop in response.history
        ]
        parsed = parse_official_entry(response.text, str(response.url), history)
        parsed["redirects"] = history
        parsed["content_sha256"] = sha256(response.content).hexdigest()
        self.result.metadata["official_entry"] = parsed
        return parsed

    async def _final_membership_recheck(self, initial_rows: list[dict]) -> dict:
        started = time.monotonic()
        result = {"complete": False}
        try:
            value, response_sha = await self._post(BOARD_OPERATIONS)
            board = parse_board_response(value)
            final_rows = board["rows"]
        except Exception as exc:
            result.update({
                "error": f"{type(exc).__name__}: {exc}",
                "duration_seconds": round(max(0.0, time.monotonic() - started), 6),
            })
            self.result.errors.append(f"final_listing_recheck_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["final_membership_recheck"] = result
            return result
        initial_ids = [row["extId"] for row in initial_rows]
        final_ids = [row["extId"] for row in final_rows]
        collected_ids = [job.job_id for job in self.result.jobs]
        changed = {}
        initial_by_id = {row["extId"]: row for row in initial_rows}
        for row in final_rows:
            before = initial_by_id.get(row["extId"])
            if before is None:
                changed[row["extId"]] = {"new": True}
                continue
            fields = {}
            if normalized_title(before.get("title")) != normalized_title(row.get("title")):
                fields["title"] = [normalized_title(before.get("title")), normalized_title(row.get("title"))]
            before_job = before.get("job") or {}
            after_job = row.get("job") or {}
            for key in ("employmentType", "locationType"):
                if before_job.get(key) != after_job.get(key):
                    fields[key] = [before_job.get(key), after_job.get(key)]
            if fields:
                changed[row["extId"]] = fields
        complete = set(initial_ids) == set(final_ids) and len(initial_ids) == len(final_ids)
        if not complete:
            self.result.errors.append("gem_listing_changed_during_run")
        result = {
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "collected_ids": collected_ids,
            "initial_count": len(initial_ids),
            "final_count": len(final_ids),
            "set_matches": complete,
            "collected_set_matches_initial": set(collected_ids) == set(initial_ids),
            "changed_fields_by_id": changed,
            "new_ids": sorted(set(final_ids) - set(initial_ids)),
            "removed_ids": sorted(set(initial_ids) - set(final_ids)),
            "final_response_sha256": response_sha,
            "complete": complete,
            "duration_seconds": round(max(0.0, time.monotonic() - started), 6),
        }
        self.result.metadata["final_membership_recheck"] = result
        self.result.timings["final_membership_recheck_seconds"] = result["duration_seconds"]
        return result

    async def _detail(self, job):
        ext_id = job.job_id
        operation = {
            "operationName": "ExternalJobPosting",
            "variables": {"boardId": BOARD_ID, "extId": ext_id},
            "query": DETAIL_QUERY,
        }
        value, response_sha = await self._post([operation])
        detail = parse_detail_response(value, ext_id, job.raw["listing"])
        fields = detail_record(detail, job.raw["listing"])
        main_description = text_html(detail.get("descriptionHtml") or "").strip()
        if not main_description:
            job.description_unavailable = True
            job.warnings.append("source_description_missing")
            self.result.errors.append(f"gem_detail_empty_description: {ext_id}")
        job.title = fields["title"]
        job.url = detail_url(ext_id)
        job.apply_url = job.url
        job.requisition_id = fields["requisition_id"]
        job.description_html = fields["description_html"]
        job.description_text = fields["description_text"]
        job.description_method = "hasura.promptql.gem.ExternalJobPosting.full_role_sections"
        job.description_source_url = API_URL
        job.locations = fields["locations"]
        job.department = fields["department"]
        job.team = fields["team"]
        job.employment_type = fields["employment_type"]
        job.workplace_type = fields["workplace_type"]
        job.date_posted = fields["date_posted"]
        job.date_posted_raw = fields["date_posted_raw"]
        job.salary = fields["salary"]
        job.qualifications = fields["qualifications"]
        job.responsibilities = fields["responsibilities"]
        job.employer = fields["employer"]
        if fields["warnings"]:
            job.warnings.extend(w for w in fields["warnings"] if w not in job.warnings)
        job.raw["detail"] = detail
        job.raw["detail_response_sha256"] = response_sha
        job.raw["posting_kind"] = fields["posting_kind"]
        job.raw["description_sections"] = fields["sections"]
        job.raw["employer_statement_present"] = fields["employer_statement_present"]
        job.raw["application_questions_excluded"] = True
        job.raw["description_components"] = {
            "introHtml": bool((detail.get("jobPostSectionHtml") or {}).get("introHtml")),
            "descriptionHtml": bool(detail.get("descriptionHtml")),
            "outroHtml": bool((detail.get("jobPostSectionHtml") or {}).get("outroHtml")),
            "compensationHtml": bool(detail.get("compensationHtml")),
        }
        job.raw["date_posted_source"] = "gem.ExternalJobPosting.firstPublishedTsSec"
        return job

    async def run(self):
        started = time.monotonic()
        if self.source.url.rstrip("/") != LISTING_URL:
            raise ValueError(f"Hasura source URL must be the observed PromptQL board: {self.source.url}")
        await self._verify_official_entry()
        if not self.page_guard():
            raise ValueError("Hasura page budget exhausted before the board request")
        value, board_sha = await self._post(BOARD_OPERATIONS)
        board = parse_board_response(value)
        self.result.metadata["board"] = {
            "id": board["board"].get("id"),
            "teamDisplayName": board["board"].get("teamDisplayName"),
            "pageTitle": board["board"].get("pageTitle"),
            "descriptionHtml": board["board"].get("descriptionHtml"),
            "filters": board["filters"],
            "filters_applied": [],
            "response_sha256": board_sha,
            "operation_names": [op["operationName"] for op in BOARD_OPERATIONS],
            "pagination": "none; one unfiltered JobBoardList response",
        }
        self.result.expected_total = board["count"]
        for row in board["rows"]:
            ext_id = row["extId"]
            job = self.job(
                {},
                title=normalized_title(row.get("title")),
                url=detail_url(ext_id),
                job_id=ext_id,
                locations=clean_locations(row.get("locations") or []),
                department=(row.get("job") or {}).get("department", {}).get("name"),
                team=(row.get("job") or {}).get("teamDisplayName") or EXPECTED_TEAM,
                employment_type=EMPLOYMENT_TYPES.get((row.get("job") or {}).get("employmentType"),
                                                      (row.get("job") or {}).get("employmentType")),
                workplace_type=WORKPLACE_TYPES.get((row.get("job") or {}).get("locationType"),
                                                    (row.get("job") or {}).get("locationType")),
                employer=EMPLOYER,
                apply_url=detail_url(ext_id),
                language="en-US",
            )
            job.raw["listing"] = row
            self.add(job)
            if self.limit():
                break
        if not self.result.jobs:
            raise ValueError("Hasura PromptQL board returned no usable jobs")
        self.result.timings["source_collection_seconds"] = round(max(0.0, time.monotonic() - started), 6)
        self.result.metadata["detail_request"] = {
            "endpoint": API_URL,
            "operation_name": "ExternalJobPosting",
            "batch_size": 1,
            "jobs_requested": len(self.result.jobs),
            "application_questions_retained_in_record": False,
        }
        await self.hydrate(self.result.jobs, self._detail)
        final = await self._final_membership_recheck(board["rows"])
        initial_ids = [row["extId"] for row in board["rows"]]
        collected_ids = [job.job_id for job in self.result.jobs]
        sample = "sample_limit" in self.result.warnings
        complete = bool(final.get("complete") and not sample and len(collected_ids) == len(initial_ids))
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": initial_ids,
            "collected_ids": collected_ids,
            "final_ids": final.get("final_ids"),
            "initial_count": len(initial_ids),
            "collected_count": len(collected_ids),
            "final_count": final.get("final_count"),
            "new_ids": final.get("new_ids", []),
            "removed_ids": final.get("removed_ids", []),
            "changed_fields_by_id": final.get("changed_fields_by_id", {}),
            "source_pages": 1,
            "pagination_controls": [],
            "api_order_is_ui_order": False,
            "complete": complete,
            "final_recheck": final,
        }
        self.result.listing_complete = complete
        if not complete and "sample_limit" not in self.result.warnings:
            if "gem_listing_changed_during_run" not in self.result.errors:
                self.result.errors.append("gem_membership_incomplete")
        self.result.metadata["application_questions_excluded"] = True
        self.result.metadata["talent_pipeline_ids"] = sorted(
            job.job_id for job in self.result.jobs if job.raw.get("posting_kind") == "talent_community"
        )
        return self.result
