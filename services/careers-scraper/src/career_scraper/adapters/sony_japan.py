"""Sony Japan's published Japan mid-career listings.

The ledger entry ``https://www.sony.com/en/SonyInfo/Careers/`` is the Sony Group
global careers page.  Live requests to ``www.sony.com`` return Akamai 403 from
this environment.  The official Japan journey (documented from archived official
pages in ``companies/sony-japan/journey-research.md``) publishes four live,
company-scoped listings:

* ``app.zookeep.com/career/sonygroupcorp`` -- Sony Group Corporation (35)
* ``app.zookeep.com/career/sonycorp`` -- Sony Corporation (1)
* ``app.zookeep.com/career/sonysemiconductorsolutions`` -- Sony Semiconductor
  Solutions Corporation (32)
* ``careers.playstation.com/ja/locations/tokyo/mid-professional/`` -- Sony
  Interactive Entertainment Tokyo (55)

This adapter never enumerates a guessed ATS board.  ZooKeep account slugs and
the SIE Japan listing come from the Sony-published links.  The Greenhouse
application links found on SIE details are retained as exact apply URLs only.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from hashlib import sha256
from html import escape
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Job, text_html


OFFICIAL_ENTRY = "https://www.sony.com/en/SonyInfo/Careers/"
JAPAN_CAREERS_DIRECTORY = (
    "https://www.sony.com/ja/SonyInfo/Jobs/careers/contents/info/detail.html"
)
ZOOKEEP_ORIGIN = "https://app.zookeep.com"
ZOOKEEP_API = ZOOKEEP_ORIGIN + "/api/public/career_site"
SIE_LISTING_URL = (
    "https://careers.playstation.com/ja/locations/tokyo/mid-professional/job"
)

ZOOKEEP_ORGS: dict[str, dict[str, str]] = {
    "sonygroupcorp": {
        "employer": "Sony Group Corporation",
        "employer_ja": "ソニーグループ株式会社",
        "published_from": JAPAN_CAREERS_DIRECTORY,
    },
    "sonycorp": {
        "employer": "Sony Corporation",
        "employer_ja": "ソニー株式会社",
        "published_from": "https://www.sony.co.jp/recruit/mid-career/",
    },
    "sonysemiconductorsolutions": {
        "employer": "Sony Semiconductor Solutions Corporation",
        "employer_ja": "ソニーセミコンダクタソリューションズ株式会社",
        "published_from": "https://www.sony-semicon.com/ja/jobs/index.html",
    },
}

ZOOKEEP_PAGE_SIZE = 25
ZOOKEEP_JOB_ID = re.compile(r"^\d+$")
ZOOKEEP_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.I)
SIE_JOB_PATH = re.compile(r"/job/(\d+)$")
SIE_PAGE_PATH = re.compile(r"/job/page/(\d+)$")
SIE_SHOWING = re.compile(
    r"(?:表示中|Showing)\s*(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*(?:求人|jobs?)",
    re.I,
)

TELEWORK_LABELS = {
    "possible": "ハイブリッド",
    "onsite": "なし",
    "remote": "リモート",
    "hybrid": "ハイブリッド",
}
EMPLOYMENT_LABELS = {
    "permanent": "正社員",
    "contract": "契約社員",
    "part_time": "パートタイム",
    "part-time": "パートタイム",
    "internship": "インターン",
    "temporary": "契約社員",
}
SOCIAL_INSURANCE_LABELS = {
    "health": "健康保険",
    "employment": "雇用保険",
    "welfare_pension": "厚生年金保険",
    "accident_compensation": "労災保険",
    "nursing_care": "介護保険",
}
DAYS_OFF_LABELS = {
    "sat_sun_off": "土日",
    "natl_holidays": "祝日",
    "year_end": "年末年始",
    "paid_leave": "有給休暇",
    "special_leave": "特別休暇",
}
LANGUAGE_LABELS = {
    "en": "英語",
    "ja": "日本語",
    "zh": "中国語",
    "ko": "韓国語",
}
LEVEL_LABELS = {1: "日常会話", 2: "ビジネス中級", 3: "ビジネス上級"}

APPLICATION_FIELDS = ("questions", "preScreeningQuestions", "applicationQuestions")
# ZooKeep localises `function.title` from Accept-Language.  The published page
# and its Japanese labels are observed with ja-JP, so request that same locale.
ZOOKEEP_HEADERS = {"Accept-Language": "ja-JP,ja;q=0.9"}
SIE_HEADERS = {"Accept-Language": "ja-JP,ja;q=0.9"}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace-normalised visible body text used by the rendered comparison."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def zookeep_org_url(org: str) -> str:
    return f"{ZOOKEEP_API}/organization/{org}/"


def zookeep_options_url(org: str) -> str:
    return f"{ZOOKEEP_API}/{org}/job_postings/options/"


def zookeep_listing_url(org: str, *, limit: int = ZOOKEEP_PAGE_SIZE, offset: int = 0) -> str:
    return (
        f"{ZOOKEEP_API}/{org}/job_postings/?search=&limit={limit}"
        f"&offset={offset}&ordering=title"
    )


def zookeep_detail_api_url(org: str, slug: str) -> str:
    return f"{ZOOKEEP_API}/{org}/job_postings/{slug}/"


def zookeep_detail_page_url(org: str, slug: str) -> str:
    return f"{ZOOKEEP_ORIGIN}/career/{org}/{slug}"


def parse_zookeep_listing(payload: dict, *, expected_org: str | None = None) -> dict:
    """Parse one ZooKeep public listing page and validate its envelope."""
    if not isinstance(payload, dict):
        raise ValueError("ZooKeep listing is not a JSON object")
    count = payload.get("count")
    rows = payload.get("results")
    if not isinstance(count, int) or count < 0:
        raise ValueError("ZooKeep listing has no valid count")
    if not isinstance(rows, list):
        raise ValueError("ZooKeep listing has no results array")
    issues: list[str] = []
    parsed: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("non_object_row")
            continue
        job_id = row.get("jobId")
        slug = clean(row.get("slug"))
        title = clean(row.get("title"))
        if not isinstance(job_id, int) or str(job_id) == "":
            issues.append("missing_job_id")
            continue
        if not ZOOKEEP_SLUG.fullmatch(slug):
            issues.append(f"invalid_slug:{job_id}")
            continue
        if not title:
            issues.append(f"missing_title:{job_id}")
            continue
        identity = str(job_id)
        if identity in seen:
            issues.append(f"duplicate_job_id:{identity}")
            continue
        seen.add(identity)
        parsed.append(row)
    return {
        "count": count,
        "next": payload.get("next"),
        "previous": payload.get("previous"),
        "rows": parsed,
        "ids": [str(row["jobId"]) for row in parsed],
        "issues": issues,
        "expected_org": expected_org,
    }


def parse_zookeep_detail(
    payload: dict,
    *,
    expected_job_id: str | None = None,
    expected_slug: str | None = None,
) -> dict:
    """Validate one exact ZooKeep detail API record."""
    if not isinstance(payload, dict):
        raise ValueError("ZooKeep detail is not a JSON object")
    job_id = payload.get("jobId")
    slug = clean(payload.get("slug"))
    title = clean(payload.get("title"))
    if not isinstance(job_id, int) or not title or not slug:
        raise ValueError("ZooKeep detail record lacks jobId/slug/title")
    if expected_job_id is not None and str(job_id) != str(expected_job_id):
        raise ValueError(
            f"ZooKeep detail jobId mismatch: expected {expected_job_id}, got {job_id}"
        )
    if expected_slug is not None and slug != expected_slug:
        raise ValueError(
            f"ZooKeep detail slug mismatch: expected {expected_slug}, got {slug}"
        )
    return payload


def _salary_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        amount = int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return clean(value)
    if amount and amount % 10000 == 0:
        return f"￥{amount // 10000}万"
    return f"￥{amount:,}"


def salary_display(record: dict) -> str:
    low = _salary_value(record.get("salaryFrom"))
    high = _salary_value(record.get("salaryTo"))
    currency = clean(record.get("salaryCurrency"))
    if low and high:
        value = f"{low} - {high}"
    else:
        value = low or high
    if not value:
        return ""
    if currency and currency != "JPY":
        value += f" {currency}"
    return value


def salary_object(record: dict) -> dict | None:
    fields = {
        "from": record.get("salaryFrom"),
        "to": record.get("salaryTo"),
        "currency": record.get("salaryCurrency"),
        "period": record.get("salaryPer"),
        "display": salary_display(record),
    }
    if any(value not in (None, "") for value in fields.values()):
        return fields
    return None


def language_lines(record: dict) -> list[str]:
    lines = []
    for item in record.get("requiredLanguages") or []:
        if not isinstance(item, dict):
            continue
        language = LANGUAGE_LABELS.get(clean(item.get("language")).lower(), clean(item.get("language")))
        level = LEVEL_LABELS.get(item.get("level"), "")
        if language and level:
            lines.append(f"{language} {level}")
        elif item.get("formatted"):
            lines.append(clean(item["formatted"]))
        elif language:
            lines.append(language)
    return lines


def _condition_pairs(record: dict) -> list[tuple[str, str]]:
    """Return the visible 働き方・条件 label/value pairs in page order."""
    pairs: list[tuple[str, str]] = []
    currency = clean(record.get("salaryCurrency"))
    if currency:
        pairs.append(("支払い通貨", "¥日本円" if currency == "JPY" else currency))
    salary = salary_display(record)
    if salary:
        periods = {"year": "年収", "month": "月収", "hour": "時給"}
        pairs.append((periods.get(clean(record.get("salaryPer")), "給与"), salary))
    bonus = clean(record.get("bonusSystem"))
    remaining_conditions: list[tuple[str, str]] = []
    conditions = record.get("jobConditions") or []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        label = clean(condition.get("label"))
        response = clean(condition.get("response"))
        if not label or not response:
            continue
        if label.startswith("賞与"):
            bonus = response
        else:
            remaining_conditions.append((label, response))
    if bonus:
        pairs.append(("賞与・その他", bonus))
    employment = clean(record.get("employmentType"))
    if employment:
        pairs.append(("雇用形態", EMPLOYMENT_LABELS.get(employment, employment)))
    location = clean(record.get("workLocation"))
    if location:
        pairs.append(("勤務地", location))
    telework = clean(record.get("teleworkEligibility"))
    if telework:
        pairs.append(("リモートワーク", TELEWORK_LABELS.get(telework, telework)))
    simple_fields = [
        ("workingHours", "勤務時間"),
        ("breakTimeMins", "休憩時間（分）"),
        ("overtimeConditions", "時間外労働"),
        ("workExperience", "経験年数"),
        ("educationalLevel", "学歴"),
        ("visaSponsorshipAvailable", "ビザスポンサー"),
        ("fixedTermContractRenewal", "契約更新"),
        ("fixedTermContractDuration", "契約期間"),
        ("probationPeriodMonths", "試用期間（月）"),
        ("smokingRule", "受動喫煙対策"),
        ("commutationAllowance", "通勤手当"),
        ("otherBenefits", "その他福利厚生"),
        ("additionalLeaves", "追加休暇"),
    ]
    for key, label in simple_fields:
        value = record.get(key)
        if value not in (None, "", [], False):
            if key == "workExperience" and clean(value) == "none":
                value = "不問"
            pairs.append((label, clean(value)))
    social = record.get("socialInsurances") or []
    if social:
        labels = [SOCIAL_INSURANCE_LABELS.get(str(item), str(item)) for item in social]
        pairs.append(("社会保険", "、".join(labels)))
    days_off = record.get("daysOff") or []
    if days_off:
        labels = [DAYS_OFF_LABELS.get(str(item), str(item)) for item in days_off]
        pairs.append(("休日", "、".join(labels)))
    skills = record.get("skills") or []
    if skills:
        pairs.append(("スキル", "、".join(clean(item) for item in skills if clean(item))))
    if record.get("paidLeaves") not in (None, "", [], False):
        pairs.append(("有給休暇", clean(record.get("paidLeaves"))))
    pairs.extend(remaining_conditions)
    return pairs


def compose_zookeep_description(record: dict) -> str:
    """Compose the full visible role description from exact ZooKeep fields.

    Application questions (``questions``/``preScreeningQuestions``/
    ``applicationQuestions``) are deliberately excluded.  Labels mirror the
    rendered detail page so a browser body comparison is meaningful.
    """
    parts: list[str] = []

    def section(label: str, html: str | None) -> None:
        if html:
            parts.append(f"<h3>{escape(label)}</h3>{html}")

    section("求人名", f"<p>{escape(clean(record.get('title')))}</p>")
    function = record.get("function")
    if isinstance(function, dict) and clean(function.get("title")):
        section("業務カテゴリ", f"<p>{escape(clean(function['title']))}</p>")
    department = clean(record.get("department"))
    if department:
        section("部署", f"<p>{escape(department)}</p>")
    section("企業理念", record.get("mission") or "")
    section("部署 / チームのミッション", record.get("departmentTeamMission") or "")
    section("募集背景", record.get("reasonForOpening") or "")
    section("業務内容", record.get("responsibilities") or "")
    section("応募資格", record.get("requirements") or "")
    languages = language_lines(record)
    if languages:
        section(
            "語学力",
            "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in languages) + "</ul>",
        )
    conditions = _condition_pairs(record)
    if conditions:
        html = "".join(
            f"<p>{escape(label)}</p><p>{escape(value)}</p>" for label, value in conditions
        )
        parts.append(f"<h3>働き方・条件</h3>{html}")
    return "\n".join(parts)


def zookeep_job(adapter: Adapter, record: dict, *, org: str, org_meta: dict) -> Job:
    job_id = str(record["jobId"])
    slug = clean(record["slug"])
    title = clean(record["title"])
    employer = clean(org_meta.get("name")) or ZOOKEEP_ORGS[org]["employer"]
    function = record.get("function")
    department = clean(function.get("title")) if isinstance(function, dict) else None
    raw_record = {
        key: value for key, value in record.items() if key not in APPLICATION_FIELDS
    }
    raw = {
        "source": "zookeep",
        "org": org,
        "org_name": employer,
        "org_name_ja": clean(org_meta.get("nameJa")),
        "org_website": clean(org_meta.get("website")),
        "listing_url": f"{ZOOKEEP_ORIGIN}/career/{org}",
        "listing_api_url": zookeep_listing_url(org),
        "detail_api_url": zookeep_detail_api_url(org, slug),
        "detail_page_url": zookeep_detail_page_url(org, slug),
        "job_id": record["jobId"],
        "slug": slug,
        "application_form_question_counts": {
            key: len(record.get(key) or []) for key in APPLICATION_FIELDS
        },
        "application_form_questions_excluded": True,
        "record": raw_record,
    }
    warnings: list[str] = []
    if "ポジションサーチ" in title:
        warnings.append("talent_pipeline_or_evergreen_intake")
    return adapter.job(
        raw,
        title=title,
        url=zookeep_detail_page_url(org, slug),
        job_id=job_id,
        description=compose_zookeep_description(record),
        method="zookeep_detail_api_full_role_fields",
        description_url=zookeep_detail_api_url(org, slug),
        apply_url=zookeep_detail_page_url(org, slug),
        locations=[clean(record["workLocation"])] if clean(record.get("workLocation")) else [],
        department=department,
        employment_type=clean(record.get("employmentType")) or None,
        workplace_type=clean(record.get("teleworkEligibility")) or None,
        salary=salary_object(record),
        qualifications={
            "requirements": record.get("requirements"),
            "education": record.get("educationalLevel"),
            "skills": record.get("skills"),
            "languages": record.get("requiredLanguages"),
            "experience": record.get("workExperience"),
        },
        responsibilities={
            "mission": record.get("mission"),
            "department_team_mission": record.get("departmentTeamMission"),
            "responsibilities": record.get("responsibilities"),
            "reason_for_opening": record.get("reasonForOpening"),
        },
        experience=clean(record.get("workExperience")) or None,
        education=clean(record.get("educationalLevel")) or None,
        employer=employer,
        language="ja",
        warnings=warnings,
    )


def parse_sie_listing(html: str, *, page_url: str) -> dict:
    """Parse one SIE Tokyo mid-professional listing page."""
    soup = BeautifulSoup(html or "", "lxml")
    text = " ".join(soup.get_text("\n", strip=True).split())
    match = SIE_SHOWING.search(text)
    if match is None:
        raise ValueError(f"SIE listing does not publish a visible total: {page_url}")
    start, end, total = (int(value) for value in match.groups())
    page_numbers = set()
    for anchor in soup.find_all("a", href=True):
        target = urlsplit(urljoin(page_url, anchor["href"]))
        page_match = SIE_PAGE_PATH.search(target.path)
        if page_match:
            page_numbers.add(int(page_match.group(1)))
    max_page = max(page_numbers) if page_numbers else 1
    rows: list[dict] = []
    seen: set[str] = set()
    issues: list[str] = []
    for anchor in soup.find_all("a", href=True):
        target = urlsplit(urljoin(page_url, anchor["href"]))
        job_match = SIE_JOB_PATH.search(target.path)
        if not job_match:
            continue
        job_id = job_match.group(1)
        title = clean(anchor.get_text(" ", strip=True))
        if title in ("Apply Now", "今すぐ応募", "応募"):
            continue
        if not title:
            continue
        if job_id in seen:
            continue
        seen.add(job_id)
        rows.append(
            {
                "job_id": job_id,
                "title": title,
                "url": urljoin(page_url, anchor["href"]),
            }
        )
    if not rows:
        issues.append(f"no_job_links:{page_url}")
    return {
        "start": start,
        "end": end,
        "total": total,
        "max_page": max_page,
        "rows": rows,
        "ids": [row["job_id"] for row in rows],
        "issues": issues,
    }


def _jsonld_jobposting(html: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        value = script.string if script.string is not None else script.get_text()
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return candidate
    raise ValueError("SIE detail page has no JobPosting JSON-LD")


def _jsonld_identifier(identifier: Any) -> str | None:
    candidates = identifier if isinstance(identifier, list) else [identifier]
    for item in candidates:
        if isinstance(item, dict) and clean(item.get("value")):
            return clean(item["value"])
    return None


def _jsonld_locations(location: Any) -> list[dict]:
    values = location if isinstance(location, list) else [location]
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        result.append(
            {
                "street": clean(address.get("streetAddress")),
                "locality": clean(address.get("addressLocality")),
                "region": clean(address.get("addressRegion")),
                "postal_code": clean(address.get("postalCode")),
                "country": clean(address.get("addressCountry")),
            }
        )
    return result


def _location_text(locations: list[dict]) -> list[str]:
    texts = []
    for item in locations:
        values = [
            item.get("locality"),
            item.get("region"),
            item.get("country"),
        ]
        value = ", ".join(value for value in values if value)
        if value:
            texts.append(value)
    return texts


def parse_sie_detail(html: str, *, page_url: str) -> dict:
    """Parse one SIE detail page's JobPosting JSON-LD and exact apply link."""
    posting = _jsonld_jobposting(html)
    description = posting.get("description")
    if not isinstance(description, str) or len(text_html(description)) < 100:
        raise ValueError(f"SIE detail has no full description: {page_url}")
    soup = BeautifulSoup(html or "", "lxml")
    apply_url = None
    for anchor in soup.find_all("a", href=True):
        label = clean(anchor.get_text(" ", strip=True))
        target = urljoin(page_url, anchor["href"])
        if label in ("今すぐ応募", "Apply Now") or "greenhouse.io" in target:
            apply_url = target
            break
    identifier = _jsonld_identifier(posting.get("identifier"))
    location_dicts = _jsonld_locations(posting.get("jobLocation"))
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(clean(item) for item in employment if clean(item))
    organization = posting.get("hiringOrganization")
    employer = clean(organization.get("name")) if isinstance(organization, dict) else ""
    return {
        "title": clean(posting.get("title")) or "",
        "description_html": description,
        "date_posted": clean(posting.get("datePosted")) or None,
        "valid_through": clean(posting.get("validThrough")) or None,
        "employment_type": clean(employment) or None,
        "identifier": identifier,
        "employer": employer or "Sony Interactive Entertainment (SIE)",
        "locations": location_dicts,
        "location_texts": _location_text(location_dicts),
        "apply_url": apply_url,
        "canonical_url": clean(posting.get("url")) or page_url,
        "direct_apply": bool(posting.get("directApply")),
        "json_ld": posting,
    }


def sie_job(adapter: Adapter, detail: dict, *, listing_url: str, listing_title: str) -> Job:
    canonical = detail["canonical_url"]
    job_id_match = SIE_JOB_PATH.search(urlsplit(canonical).path)
    job_id = job_id_match.group(1) if job_id_match else ""
    title = detail["title"] or listing_title
    warnings = []
    if "オープンポジション" in title or "Open Position" in title:
        warnings.append("open_position_evergreen_intake")
    raw = {
        "source": "sie",
        "listing_url": listing_url,
        "listing_title": listing_title,
        "apply_url": detail.get("apply_url"),
        "direct_apply": detail.get("direct_apply"),
        "json_ld": detail["json_ld"],
    }
    return adapter.job(
        raw,
        title=title,
        url=canonical,
        job_id=job_id or detail.get("identifier"),
        description=detail["description_html"],
        method="sie_jsonld_jobposting_description",
        description_url=canonical,
        apply_url=detail.get("apply_url"),
        date_posted=detail.get("date_posted"),
        date_posted_raw=detail.get("date_posted"),
        valid_through=detail.get("valid_through"),
        locations=detail.get("location_texts") or [],
        employment_type=detail.get("employment_type"),
        requisition_id=detail.get("identifier"),
        employer=detail.get("employer"),
        language="ja",
        warnings=warnings,
    )


class SonyJapan(Adapter):
    """Collect the four Sony-published Japan mid-career listings."""

    def _kind(self) -> str:
        kind = self.source.options.get("kind")
        if kind:
            return str(kind)
        host = urlsplit(self.source.url).netloc.lower()
        if host == "app.zookeep.com":
            return "zookeep"
        if host == "careers.playstation.com":
            return "sie"
        raise ValueError(f"Sony Japan source is not a published listing: {self.source.url}")

    async def run(self):
        kind = self._kind()
        if kind == "zookeep":
            return await self._run_zookeep()
        if kind == "sie":
            return await self._run_sie()
        raise ValueError(f"Unknown Sony Japan source kind: {kind}")

    async def _run_zookeep(self):
        started = time.perf_counter()
        org = clean(self.source.options.get("org"))
        if org not in ZOOKEEP_ORGS:
            raise ValueError(f"Unexpected ZooKeep account for Sony Japan: {org!r}")
        org_meta = await self.http.json(zookeep_org_url(org), headers=ZOOKEEP_HEADERS)
        if not isinstance(org_meta, dict) or clean(org_meta.get("name")) != ZOOKEEP_ORGS[org]["employer"]:
            raise ValueError(f"ZooKeep account identity changed for {org}")
        options = await self.http.json(zookeep_options_url(org), headers=ZOOKEEP_HEADERS)
        self.result.metadata["zookeep_options"] = options

        listing_started = time.perf_counter()
        rows: list[dict] = []
        ids: list[str] = []
        page_counts: list[int] = []
        counts: set[int] = set()
        next_url = zookeep_listing_url(org, limit=ZOOKEEP_PAGE_SIZE, offset=0)
        issues: list[str] = []
        pages = 0
        while next_url:
            if not self.page_guard():
                break
            payload = await self.http.json(next_url, headers=ZOOKEEP_HEADERS)
            parsed = parse_zookeep_listing(payload, expected_org=org)
            pages += 1
            counts.add(parsed["count"])
            page_counts.append(len(parsed["rows"]))
            issues.extend(parsed["issues"])
            for row in parsed["rows"]:
                identity = str(row["jobId"])
                if identity in ids:
                    issues.append(f"duplicate_across_pages:{identity}")
                    continue
                ids.append(identity)
                rows.append(row)
            next_url = parsed.get("next")
            if next_url and not isinstance(next_url, str):
                issues.append("invalid_next_link")
                break
        listing_seconds = max(0.0, time.perf_counter() - listing_started)

        if len(counts) != 1:
            issues.append(f"inconsistent_count_across_pages:{sorted(counts)}")
        expected = next(iter(counts)) if len(counts) == 1 else None
        if expected is not None and len(ids) != expected:
            issues.append(f"listing_id_count_mismatch:{len(ids)}!={expected}")
        if expected is not None and sum(page_counts) != expected:
            issues.append(f"page_row_sum_mismatch:{sum(page_counts)}!={expected}")

        jobs = []
        for row in rows:
            job = zookeep_job(self, row, org=org, org_meta=org_meta)
            job.raw["listing_row"] = _listing_summary(row)
            self.add(job)
            jobs.append(job)
        self.result.jobs = jobs

        detail_started = time.perf_counter()
        detail_errors: list[str] = []

        async def detail(job: Job) -> Job:
            slug = clean(job.raw.get("slug"))
            expected_job_id = str(job.job_id)
            try:
                payload = await self.http.json(zookeep_detail_api_url(org, slug), headers=ZOOKEEP_HEADERS)
                record = parse_zookeep_detail(
                    payload,
                    expected_job_id=expected_job_id,
                    expected_slug=slug,
                )
            except Exception as exc:
                detail_errors.append(f"{expected_job_id}:{type(exc).__name__}:{exc}")
                raise
            hydrated = zookeep_job(self, record, org=org, org_meta=org_meta)
            hydrated.raw["listing_row"] = job.raw.get("listing_row")
            hydrated.raw["detail_record_sha256"] = sha256(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            return hydrated

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.expected_total = expected
        self.result.timings["source_collection_seconds"] = listing_seconds
        self.result.timings["detail_fetch_seconds"] = detail_seconds
        self.result.metadata.update(
            {
                "source_kind": "zookeep",
                "org": org,
                "employer": clean(org_meta.get("name")),
                "employer_ja": clean(org_meta.get("nameJa")),
                "employer_website": clean(org_meta.get("website")),
                "published_from": ZOOKEEP_ORGS[org]["published_from"],
                "listing_url": f"{ZOOKEEP_ORIGIN}/career/{org}",
                "listing_api_url": zookeep_listing_url(org),
                "listing_pages": pages,
                "page_row_counts": page_counts,
                "count": expected,
                "listing_ids": ids,
                "detail_api_requests": len(rows),
                "detail_failures": detail_errors,
                "application_questions_excluded": list(APPLICATION_FIELDS),
                "posting_dates_published": False,
            }
        )
        reconciliation = self._reconcile(expected, ids, issues)
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.listing_complete = reconciliation["complete"]
        self.result.pages = pages
        if issues:
            self.result.errors.extend(f"zookeep:{issue}" for issue in issues)
        if detail_errors:
            self.result.errors.extend(f"zookeep_detail:{error}" for error in detail_errors)
        self.result.timings["reconciliation_seconds"] = max(
            0.0, time.perf_counter() - started - listing_seconds - detail_seconds
        )
        return self.result

    async def _run_sie(self):
        started = time.perf_counter()
        listing_started = time.perf_counter()
        first = await self.http.get(SIE_LISTING_URL, headers=SIE_HEADERS)
        pages_html = [first.text]
        parsed_pages = [parse_sie_listing(first.text, page_url=SIE_LISTING_URL)]
        total = parsed_pages[0]["total"]
        max_page = parsed_pages[0]["max_page"]
        issues = list(parsed_pages[0]["issues"])
        for page in range(2, max_page + 1):
            if not self.page_guard():
                break
            url = f"{SIE_LISTING_URL}/page/{page}"
            response = await self.http.get(url, headers=SIE_HEADERS)
            pages_html.append(response.text)
            parsed = parse_sie_listing(response.text, page_url=url)
            if parsed["total"] != total:
                issues.append(f"total_changed_on_page_{page}:{parsed['total']}!={total}")
            issues.extend(parsed["issues"])
            parsed_pages.append(parsed)
        listing_seconds = max(0.0, time.perf_counter() - listing_started)

        rows: list[dict] = []
        ids: list[str] = []
        page_counts: list[int] = []
        for parsed in parsed_pages:
            page_counts.append(len(parsed["rows"]))
            for row in parsed["rows"]:
                if row["job_id"] in ids:
                    issues.append(f"duplicate_across_pages:{row['job_id']}")
                    continue
                ids.append(row["job_id"])
                rows.append(row)
        if len(ids) != total:
            issues.append(f"listing_id_count_mismatch:{len(ids)}!={total}")
        if sum(page_counts) != total:
            issues.append(f"page_row_sum_mismatch:{sum(page_counts)}!={total}")
        if len(parsed_pages) != max_page:
            issues.append(f"page_fetch_incomplete:{len(parsed_pages)}!={max_page}")

        jobs = []
        for row in rows:
            job = self.job(
                {"source": "sie", "listing_row": row, "listing_url": SIE_LISTING_URL},
                title=row["title"],
                url=row["url"],
                job_id=row["job_id"],
                description="",
                method="sie_detail_pending",
                description_url=row["url"],
                apply_url=row["url"],
            )
            self.add(job)
            jobs.append(job)
        self.result.jobs = jobs

        detail_started = time.perf_counter()
        detail_errors: list[str] = []

        async def detail(job: Job) -> Job:
            expected_id = str(job.job_id)
            try:
                response = await self.http.get(job.url, headers=SIE_HEADERS)
                parsed = parse_sie_detail(response.text, page_url=str(response.url))
            except Exception as exc:
                detail_errors.append(f"{expected_id}:{type(exc).__name__}:{exc}")
                raise
            hydrated = sie_job(
                self,
                parsed,
                listing_url=SIE_LISTING_URL,
                listing_title=job.raw.get("listing_row", {}).get("title", job.title),
            )
            if hydrated.job_id != expected_id:
                detail_errors.append(
                    f"{expected_id}:detail_job_id_mismatch:{hydrated.job_id}"
                )
                raise ValueError(
                    f"SIE detail job id mismatch: expected {expected_id}, got {hydrated.job_id}"
                )
            hydrated.raw["listing_row"] = job.raw.get("listing_row")
            return hydrated

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.expected_total = total
        self.result.timings["source_collection_seconds"] = listing_seconds
        self.result.timings["detail_fetch_seconds"] = detail_seconds
        self.result.metadata.update(
            {
                "source_kind": "sie",
                "employer": "Sony Interactive Entertainment (SIE)",
                "listing_url": SIE_LISTING_URL,
                "listing_pages": max_page,
                "page_row_counts": page_counts,
                "count": total,
                "listing_ids": ids,
                "detail_page_requests": len(rows),
                "detail_failures": detail_errors,
                "apply_ats": "Greenhouse exact links only; board not enumerated",
                "posting_dates_published": True,
            }
        )
        reconciliation = self._reconcile(total, ids, issues)
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.listing_complete = reconciliation["complete"]
        self.result.pages = max_page
        if issues:
            self.result.errors.extend(f"sie:{issue}" for issue in issues)
        if detail_errors:
            self.result.errors.extend(f"sie_detail:{error}" for error in detail_errors)
        self.result.timings["reconciliation_seconds"] = max(
            0.0, time.perf_counter() - started - listing_seconds - detail_seconds
        )
        return self.result

    def _reconcile(self, expected: int | None, ids: list[str], issues: list[str]) -> dict:
        jobs = self.result.jobs
        collected_ids = [str(job.job_id) for job in jobs]
        missing = [identity for identity in ids if identity not in collected_ids]
        extra = [identity for identity in collected_ids if identity not in ids]
        duplicates = sorted({identity for identity in collected_ids if collected_ids.count(identity) > 1})
        complete = bool(
            expected is not None
            and len(ids) == expected
            and len(collected_ids) == expected
            and not missing
            and not extra
            and not duplicates
            and not issues
        )
        return {
            "expected_total": expected,
            "listing_ids": ids,
            "collected_ids": collected_ids,
            "listing_id_count": len(ids),
            "collected_count": len(collected_ids),
            "missing_ids": missing,
            "extra_ids": extra,
            "duplicate_collected_ids": duplicates,
            "listing_issues": issues,
            "complete": complete,
        }


def _listing_summary(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "id",
            "jobId",
            "slug",
            "title",
            "function",
            "employmentType",
            "workLocation",
        )
    }
