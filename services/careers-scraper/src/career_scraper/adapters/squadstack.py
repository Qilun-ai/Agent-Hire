"""SquadStack's own Webflow careers page, its full-description JSON feed, and the exact Keka detail/apply route."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.squadstack.com/careers"
OFFICIAL_FINAL = "https://www.squadstack.ai/careers"
OFFICIAL_HOSTS = {"www.squadstack.ai", "squadstack.ai"}
LISTING_FEED = "https://careers.squadstack.com/webflow.php"
JOB_PAGE = "https://www.squadstack.ai/job-description?id={identity}"
KEKA_ORIGIN = "https://squadrun.keka.com"
KEKA_CAREERS = KEKA_ORIGIN + "/careers"
KEKA_HOST = "squadrun.keka.com"
UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
KEKA_JOB_PATH = re.compile(r"^/careers/jobdetails/(\d+)/?$", re.I)
PORTAL_FETCH_RE = re.compile(
    r"fetch\s*\(\s*(['\"])(/ats/documents/[0-9a-f-]+/careerportal/[0-9a-f]+\.html)\1\s*\)",
    re.I,
)
KH_CONFIG_RE = re.compile(r"window\.khConfig\s*=\s*\{(.*?)\}", re.S)
JOB_TYPE_LABELS = {1: "Part-Time", 2: "Full-Time"}
SALARY_PERIOD_LABELS = {0: "Not Available", 1: "Hourly", 2: "Bi Weekly", 3: "Monthly", 4: "Annual"}
EMPLOYER = "Squadrun Solutions Pvt Ltd"
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
KNOWN_HEADINGS = {
    "role overview", "about the role", "job overview", "what you will do",
    "what you'll do", "what you’ll do", "what you'll be doing", "what you’ll be doing",
    "responsibilities", "roles & responsibilities", "requirements", "must have",
    "must-haves", "preferred", "preferred experience", "good to have", "nice-to-haves",
    "why join us", "why join us?", "why should you consider us seriously?",
    "why ambitious engineers choose this role", "logistics", "please note", "please note:",
    "skills", "education & experience", "skills & attributes", "narrative",
    "owned media and pr", "events", "offline", "team and budget", "sell", "lead",
    "run it", "write", "produce", "publish and grow", "build the system",
    "about squadstack.ai", "about squadstack.ai :", "what success looks like",
}
RESPONSIBILITY_RE = re.compile(
    r"responsibilit|what you(?:'|’)?ll do|what you will do|what you(?:'|’)?ll be doing|"
    r"roles? (?:&|and) responsibilities|the role|your role|role overview|job overview|about the role",
    re.I,
)
QUALIFICATION_RE = re.compile(
    r"requirement|qualification|what we(?:'|’)?re looking for|what we want|must[- ]?have|"
    r"preferred|skills|who you are|education|experience|good to have|nice[- ]to[- ]have|"
    r"strong signal|technical baseline|mindset fit|what you should have", re.I,
)
END_SECTION_RE = re.compile(
    r"why (?:join|should)|logistics|please note|what you can expect|should i apply|"
    r"day-to-day|quick check|what success looks like", re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def visible_text(html: str | None) -> str:
    """Whitespace-normalised text with the source's own punctuation/quotes kept."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,;:!?])", r"\1", value)


def comparison_text(html: str | None) -> str:
    """Stable body comparison: ignore quote style and whitespace around punctuation."""
    value = visible_text(html)
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return value.replace("'", "").replace('"', "").strip()


def alphanumeric_text(value: str | None) -> str:
    """Rendered-vs-source comparison key that ignores CSS casing, punctuation and emoji spacing."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def json_fingerprint(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def same_datetime(left, right) -> bool:
    a, b = parse_datetime(left), parse_datetime(right)
    return a is not None and b is not None and a == b


def official_job_url(identity: str) -> str:
    return JOB_PAGE.format(identity=identity)


def keka_id_from_webflow_row(row: dict) -> str | None:
    value = row.get("url")
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != KEKA_HOST:
        return None
    match = KEKA_JOB_PATH.fullmatch(parts.path)
    return match.group(1) if match else None


def keka_jobdetail_url(identity) -> str:
    return f"{KEKA_ORIGIN}/careers/jobdetails/{identity}"


def keka_apply_url(identity) -> str:
    return f"{KEKA_ORIGIN}/careers/applyjob/{identity}"


def parse_official_entry(html: str, response_url: str = OFFICIAL_FINAL) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname not in OFFICIAL_HOSTS
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("SquadStack official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "squadstack" not in title.lower() or not (
        "open positions" in title.lower() or "career" in title.lower()
    ):
        raise ValueError("SquadStack official careers page identity/title changed")
    headings = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")]
    if not any("open positions" in heading.lower() for heading in headings):
        raise ValueError("SquadStack official careers page no longer shows its Open Positions heading")

    scripts = [node.get_text("\n", strip=False) for node in soup.find_all("script")
               if "careers.squadstack.com/webflow.php" in (node.get_text("\n", strip=False) or "")]
    if len(scripts) != 1:
        raise ValueError(f"SquadStack careers page no longer exposes exactly one webflow.php listing script ({len(scripts)})")
    feed_matches = sorted(set(re.findall(r"https://careers\.squadstack\.com/webflow\.php", scripts[0])))
    if feed_matches != [LISTING_FEED]:
        raise ValueError(f"SquadStack careers page listing feed changed: {feed_matches}")

    preferred_match = re.search(r"var\s+PreferredArr\s*=\s*\[(.*?)\]\s*;", scripts[0], re.S)
    if preferred_match is None:
        raise ValueError("SquadStack careers page no longer exposes its preferred-job order")
    preferred_ids = re.findall(r"[\"']([0-9a-f-]{36})[\"']", preferred_match.group(1), re.I)
    if not preferred_ids or any(not UUID_RE.fullmatch(identity) for identity in preferred_ids):
        raise ValueError("SquadStack preferred-job IDs are not UUIDs")

    filters = [normalized_title(node.get_text(" ", strip=True))
               for node in soup.select(".dropdown-toggle-5 .checkbox-label-text")]
    if "All Teams" not in filters or "All Locations" not in filters:
        raise ValueError(f"SquadStack careers page filter controls changed: {filters}")

    return {
        "url": response_url,
        "title": title,
        "open_positions_heading": next(h for h in headings if "open positions" in h.lower()),
        "listing_feed_url": feed_matches[0],
        "preferred_ids": preferred_ids,
        "filters": filters,
        "iframe_count": len(soup.select("iframe")),
        "canonical_url": next((node.get("href") for node in soup.select('link[rel="canonical"]')), None),
    }


def parse_listing_feed(payload) -> dict:
    if not isinstance(payload, list):
        raise ValueError("SquadStack webflow.php listing is not a JSON array")
    rows, seen, keka_ids, issues = [], set(), [], []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            issues.append(f"SquadStack listing row {index} is not an object")
            continue
        identity = str(row.get("id") or "")
        if not UUID_RE.fullmatch(identity):
            issues.append(f"SquadStack listing row {index} has no UUID id")
            continue
        if identity in seen:
            issues.append(f"SquadStack listing repeats UUID {identity}")
            continue
        title = normalized_title(row.get("name"))
        if not title:
            issues.append(f"SquadStack listing {identity} has no title")
            continue
        keka_id = keka_id_from_webflow_row(row)
        if keka_id is None:
            issues.append(f"SquadStack listing {identity} has an unexpected Keka detail URL: {row.get('url')!r}")
            continue
        if keka_id in keka_ids:
            issues.append(f"SquadStack listing repeats Keka job id {keka_id}")
            continue
        description = row.get("description")
        if not isinstance(description, str) or not description.strip():
            issues.append(f"SquadStack listing {identity} has no description")
        elif len(visible_text(description)) < 100:
            issues.append(f"SquadStack listing {identity} description is implausibly short")
        if parse_datetime(row.get("published_date")) is None:
            issues.append(f"SquadStack listing {identity} has no valid published_date")
        department = normalized_title(row.get("department"))
        if not department:
            issues.append(f"SquadStack listing {identity} has no department")
        locations = row.get("locations")
        if not isinstance(locations, list) or not locations:
            issues.append(f"SquadStack listing {identity} has no locations")
        else:
            for location in locations:
                if not isinstance(location, dict) or not normalized_title(location.get("name")):
                    issues.append(f"SquadStack listing {identity} has an unidentified location")
                    break
        type_value = row.get("type")
        if not isinstance(type_value, dict) or not normalized_title(type_value.get("name")):
            issues.append(f"SquadStack listing {identity} has no listing employment type")
        if not isinstance(row.get("employement_type"), str):
            issues.append(f"SquadStack listing {identity} has no detail employment type")
        seen.add(identity)
        keka_ids.append(keka_id)
        rows.append(dict(row))
    return {
        "rows": rows,
        "count": len(rows),
        "ids": [str(row["id"]) for row in rows],
        "keka_ids": keka_ids,
        "issues": issues,
    }


def parse_keka_careers_landing(html: str, response_url: str = KEKA_CAREERS) -> str:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != KEKA_HOST or parts.path.rstrip("/") != "/careers":
        raise ValueError("SquadStack Keka careers landing is not the inspected company tenant")
    paths = sorted({match[1] if isinstance(match, tuple) else match
                    for match in PORTAL_FETCH_RE.findall(html)})
    if len(paths) != 1:
        raise ValueError(f"SquadStack Keka landing no longer exposes exactly one careerportal fetch ({len(paths)})")
    portal_url = urljoin(response_url, paths[0])
    portal_parts = urlsplit(portal_url)
    if (portal_parts.scheme != "https" or portal_parts.hostname != KEKA_HOST
            or not portal_parts.path.startswith("/ats/documents/")
            or not portal_parts.path.endswith(".html")):
        raise ValueError(f"SquadStack Keka careerportal document left the inspected tenant: {portal_url}")
    return portal_url


def parse_keka_portal(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != KEKA_HOST
            or not parts.path.startswith("/ats/documents/")
            or not parts.path.endswith(".html")):
        raise ValueError("SquadStack Keka careerportal response came from an unexpected URL")
    match = KH_CONFIG_RE.search(html)
    if match is None:
        raise ValueError("SquadStack Keka careerportal no longer exposes window.khConfig")
    body = match.group(1)
    identifier_match = re.search(r"identifier\s*:\s*['\"]([^'\"]+)['\"]", body)
    domain_match = re.search(r"domain\s*:\s*['\"]([^'\"]+)['\"]", body)
    target_match = re.search(r"targetContainer\s*:\s*['\"]([^'\"]+)['\"]", body)
    if identifier_match is None or domain_match is None:
        raise ValueError("SquadStack Keka khConfig is missing identifier/domain")
    identifier = identifier_match.group(1).strip()
    domain = domain_match.group(1).strip()
    if not UUID_RE.fullmatch(identifier):
        raise ValueError(f"SquadStack Keka khConfig identifier is not a UUID: {identifier!r}")
    domain_parts = urlsplit(domain)
    if (domain_parts.scheme != "https" or domain_parts.hostname != KEKA_HOST
            or domain_parts.path.rstrip("/") != "/careers"):
        raise ValueError(f"SquadStack Keka khConfig domain is not the inspected company tenant: {domain!r}")
    if not domain.endswith("/"):
        domain += "/"
    return {
        "identifier": identifier,
        "domain": domain,
        "target_container": target_match.group(1) if target_match else None,
    }


def parse_keka_active(payload) -> dict:
    if not isinstance(payload, list):
        raise ValueError("SquadStack Keka active-job feed is not a JSON array")
    rows, seen, issues = [], set(), []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            issues.append(f"SquadStack Keka row {index} is not an object")
            continue
        identity = row.get("id")
        if not isinstance(identity, int) or identity <= 0:
            issues.append(f"SquadStack Keka row {index} has no positive integer id")
            continue
        if identity in seen:
            issues.append(f"SquadStack Keka feed repeats job id {identity}")
            continue
        if not normalized_title(row.get("title")):
            issues.append(f"SquadStack Keka {identity} has no title")
        description = row.get("description")
        if not isinstance(description, str) or not description.strip():
            issues.append(f"SquadStack Keka {identity} has no description")
        elif len(visible_text(description)) < 100:
            issues.append(f"SquadStack Keka {identity} description is implausibly short")
        if not isinstance(row.get("jobLocations"), list) or not row.get("jobLocations"):
            issues.append(f"SquadStack Keka {identity} has no jobLocations")
        if not isinstance(row.get("jobType"), int):
            issues.append(f"SquadStack Keka {identity} has no integer jobType")
        if not isinstance(row.get("experience"), str):
            issues.append(f"SquadStack Keka {identity} has no experience field")
        if not isinstance(row.get("salaryRange"), dict):
            issues.append(f"SquadStack Keka {identity} has no salaryRange object")
        if parse_datetime(row.get("publishedOn")) is None:
            issues.append(f"SquadStack Keka {identity} has no valid publishedOn")
        if not isinstance(row.get("skillNames"), list):
            issues.append(f"SquadStack Keka {identity} has no skillNames list")
        seen.add(identity)
        rows.append(dict(row))
    return {"rows": rows, "count": len(rows), "ids": [int(row["id"]) for row in rows], "issues": issues}


def parse_keka_detail(html: str, expected_row: dict, expected_url: str) -> dict:
    parts = urlsplit(expected_url)
    match = KEKA_JOB_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.hostname != KEKA_HOST or match is None
            or match.group(1) != str(expected_row.get("id"))):
        raise ValueError("SquadStack Keka representative detail URL is not the exact exposed job route")
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    title = normalized_title(h1.get_text(" ", strip=True)) if h1 else ""
    if title != normalized_title(expected_row.get("title")):
        raise ValueError("SquadStack Keka detail title differs from the active feed")
    description = soup.select_one(".job-description-container")
    if description is None or not description.decode_contents().strip():
        raise ValueError("SquadStack Keka detail page has no server-rendered description body")
    body_html = description.decode_contents()
    if comparison_text(body_html) != comparison_text(expected_row.get("description")):
        raise ValueError("SquadStack Keka detail body differs from the active-feed body")
    apply_button = next(
        (button.get_text(" ", strip=True) for button in soup.select("button")
         if "apply" in button.get_text(" ", strip=True).lower()),
        None,
    )
    if not apply_button:
        raise ValueError("SquadStack Keka detail page has no apply action")
    headings = [normalized_title(node.get_text(" ", strip=True))
                for node in description.select("h1,h2,h3,h4")
                if normalized_title(node.get_text(" ", strip=True))]
    return {
        "url": expected_url,
        "title": title,
        "description_html": body_html,
        "description_text": visible_text(body_html),
        "description_normalized_chars": len(visible_text(body_html)),
        "description_comparison_chars": len(comparison_text(body_html)),
        "description_fnv1a64": fnv1a64(visible_text(body_html)),
        "description_alphanumeric_fnv1a64": fnv1a64(alphanumeric_text(visible_text(body_html))),
        "headings": headings,
        "apply_control": apply_button,
        "matches_active_feed": True,
    }


def ui_order(rows: list[dict], preferred_ids: list[str]) -> list[dict]:
    """Mirror the observed careers script: preferred first, then newest, then group by team."""
    by_id = {str(row.get("id")): row for row in rows}
    preferred = [by_id[identity] for identity in preferred_ids if identity in by_id]
    preferred_set = {str(row.get("id")) for row in preferred}
    remaining = [row for row in rows if str(row.get("id")) not in preferred_set]
    remaining.sort(key=lambda row: parse_datetime(row.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
    ordered = preferred + remaining
    groups: dict[str, list[dict]] = {}
    for row in ordered:
        department = normalized_title(row.get("department")) or "Other"
        groups.setdefault(department, []).append(row)
    return [row for department in groups for row in groups[department]]


def salary_from_row(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    salary_range = row.get("salaryRange")
    source_text = row.get("salaryRangeFormat")
    has_amount = isinstance(salary_range, dict) and (
        salary_range.get("minimum") not in (None, 0)
        or salary_range.get("maximum") not in (None, 0)
    )
    zero_text = bool(re.fullmatch(
        r"\s*INR\s*0(?:\.0+)?\s*-\s*0(?:\.0+)?\s*", source_text or "", re.I
    ))
    if not has_amount and (not source_text or zero_text):
        return None
    period_code = salary_range.get("salaryPeriod") if isinstance(salary_range, dict) else None
    return {
        "source_text": source_text or None,
        "range": salary_range,
        "salary_period": SALARY_PERIOD_LABELS.get(period_code, period_code),
        "salary_period_code": period_code,
    }


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-squadstack-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-squadstack-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []

    def heading_for(block: Tag) -> str:
        text = normalized_title(block.get_text(" ", strip=True))
        if not text:
            return ""
        if block.name in HEADING_TAGS:
            return text
        if block.name not in {"div", "p"}:
            return ""
        if block.find(["div", "ul", "ol", "li", "p", *HEADING_TAGS]):
            return ""
        if len(text) > 90:
            return ""
        if block.find(["strong", "b"]) or text.endswith(":") or text.lower() in KNOWN_HEADINGS:
            return text
        return ""

    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = heading_for(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": html or "", "text": ""})
    for section in sections:
        if not section["text"]:
            section["text"] = visible_text(section["html"])
    return sections


def selected_sections(sections: list[dict], start: re.Pattern, *,
                      stop: re.Pattern | None = None) -> list[dict] | None:
    selected, active = [], False
    for section in sections:
        heading = section.get("heading") or ""
        text = section.get("text") or ""
        if start.search(heading):
            active = True
            if text:
                selected.append({"heading": heading, "text": text})
            continue
        if not active:
            continue
        if stop is not None and stop.search(heading):
            break
        if text:
            selected.append({"heading": heading, "text": text})
    return selected or None


class SquadStack(Adapter):
    """Read only SquadStack's own Webflow listing and its exact company-scoped Keka jobs."""

    VERIFICATION_REVISION = "squadstack.official-webflow-feed-keka-active.1"

    async def _entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_FINAL:
            raise ValueError(f"SquadStack official careers entry redirected to an unexpected URL: {response.url}")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _listing_feed(self, label: str):
        started = time.perf_counter()
        response = await self.http.get(LISTING_FEED)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_FEED:
            raise ValueError(f"SquadStack {label} listing feed redirected away from its inspected URL")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"SquadStack {label} listing feed is not JSON") from exc
        parsed = parse_listing_feed(payload)
        parsed.update(
            label=label,
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _keka_board(self, origins: set[str]):
        if origins != {KEKA_ORIGIN}:
            raise ValueError(f"SquadStack published job links point at an unexpected Keka origin: {sorted(origins)}")
        started = time.perf_counter()
        landing = await self.http.get(KEKA_CAREERS)
        landing_seconds = max(0.0, time.perf_counter() - started)
        if str(landing.url).rstrip("/") != KEKA_CAREERS:
            raise ValueError("SquadStack Keka careers landing redirected away from the inspected tenant")
        portal_url = parse_keka_careers_landing(landing.text, str(landing.url))

        portal_started = time.perf_counter()
        portal = await self.http.get(portal_url)
        portal_seconds = max(0.0, time.perf_counter() - portal_started)
        if str(portal.url) != portal_url:
            raise ValueError("SquadStack Keka careerportal document redirected away from its inspected URL")
        kh = parse_keka_portal(portal.text, str(portal.url))
        identifier = kh["identifier"]

        info_url = urljoin(kh["domain"], "api/organization/default/careerportalinfo")
        info_started = time.perf_counter()
        info_response = await self.http.get(info_url, headers={"Referer": KEKA_CAREERS})
        info_seconds = max(0.0, time.perf_counter() - info_started)
        if str(info_response.url) != info_url:
            raise ValueError("SquadStack Keka careerportalinfo redirected away from its inspected URL")
        info = info_response.json()
        if not isinstance(info, dict) or normalized_title(info.get("shortName")) != "SquadStack":
            raise ValueError(f"SquadStack Keka careerportalinfo no longer identifies the company: {info!r}")

        embedjs_url = urljoin(kh["domain"], f"api/embedjobs/js/{identifier}")
        js_started = time.perf_counter()
        embedjs = await self.http.get(embedjs_url)
        js_seconds = max(0.0, time.perf_counter() - js_started)
        if str(embedjs.url) != embedjs_url:
            raise ValueError("SquadStack Keka embedjobs script redirected away from its inspected URL")
        if "api/embedjobs/${portalName}/active/" not in embedjs.text:
            raise ValueError("SquadStack Keka embedjobs script no longer exposes the active-job endpoint")
        active_url = urljoin(kh["domain"], f"api/embedjobs/default/active/{identifier}")

        active_started = time.perf_counter()
        active_response = await self.http.get(active_url, headers={"Referer": KEKA_CAREERS})
        active_seconds = max(0.0, time.perf_counter() - active_started)
        if str(active_response.url) != active_url:
            raise ValueError("SquadStack Keka active-job feed redirected away from its inspected URL")
        try:
            active = parse_keka_active(active_response.json())
        except ValueError as exc:
            raise ValueError(f"SquadStack Keka active-job feed is not JSON: {exc}") from exc
        active.update(
            status=active_response.status_code,
            seconds=active_seconds,
            response_sha256=sha256(active_response.content).hexdigest(),
            response_bytes=len(active_response.content),
        )
        return {
            "careers_url": str(landing.url),
            "careers_status": landing.status_code,
            "careers_seconds": landing_seconds,
            "careers_sha256": sha256(landing.content).hexdigest(),
            "careers_bytes": len(landing.content),
            "portal_url": portal_url,
            "portal_status": portal.status_code,
            "portal_seconds": portal_seconds,
            "portal_sha256": sha256(portal.content).hexdigest(),
            "portal_bytes": len(portal.content),
            "identifier": identifier,
            "domain": kh["domain"],
            "target_container": kh["target_container"],
            "portalinfo_url": info_url,
            "portalinfo_status": info_response.status_code,
            "portalinfo_seconds": info_seconds,
            "portalinfo_sha256": sha256(info_response.content).hexdigest(),
            "portalinfo": info,
            "embedjs_url": embedjs_url,
            "embedjs_status": embedjs.status_code,
            "embedjs_seconds": js_seconds,
            "embedjs_sha256": sha256(embedjs.content).hexdigest(),
            "active_url": active_url,
            "active": active,
        }

    def _make_job(self, *, webflow_row: dict | None, keka_row: dict | None, feed_url: str,
                  keka_active_url: str | None, membership_source: str):
        if webflow_row is not None:
            identity = str(webflow_row["id"])
            keka_id = keka_id_from_webflow_row(webflow_row)
            title = normalized_title(webflow_row.get("name"))
            description_html = webflow_row.get("description") or ""
            description_url = feed_url
            description_method = "squadstack.webflow_feed.description"
            job_url = official_job_url(identity)
            apply_url = keka_apply_url(keka_id)
            date_posted = webflow_row.get("published_date")
            date_posted_raw = {
                "webflow_published_date": webflow_row.get("published_date"),
                "keka_publishedOn": keka_row.get("publishedOn") if keka_row else None,
            }
            locations = list(webflow_row.get("locations") or [])
            department = normalized_title(webflow_row.get("department")) or None
            employment_type = normalized_title(webflow_row.get("employement_type")) or None
            listing_type = normalized_title((webflow_row.get("type") or {}).get("name")) or None
            record_identity = f"squadstack:{identity}"
        else:
            assert keka_row is not None
            identity = str(keka_row["id"])
            keka_id = identity
            title = normalized_title(keka_row.get("title"))
            description_html = keka_row.get("description") or ""
            description_url = keka_active_url
            description_method = "squadstack.keka_active.description"
            job_url = keka_jobdetail_url(keka_id)
            apply_url = keka_apply_url(keka_id)
            date_posted = keka_row.get("publishedOn")
            date_posted_raw = {"keka_publishedOn": keka_row.get("publishedOn")}
            locations = list(keka_row.get("jobLocations") or [])
            department = normalized_title(keka_row.get("departmentName")) or None
            employment_type = JOB_TYPE_LABELS.get(keka_row.get("jobType"), str(keka_row.get("jobType")))
            listing_type = employment_type
            record_identity = f"squadstack:keka:{identity}"

        description_text = visible_text(description_html)
        sections = description_sections(description_html)
        responsibilities = selected_sections(
            sections, RESPONSIBILITY_RE, stop=re.compile(
                f"{QUALIFICATION_RE.pattern}|{END_SECTION_RE.pattern}", re.I
            )
        )
        qualifications = selected_sections(sections, QUALIFICATION_RE, stop=END_SECTION_RE)
        job = self.job(
            {
                "webflow_listing_row": webflow_row,
                "keka_active_row": keka_row,
                "membership_source": membership_source,
                "urls": {
                    "official_job_page": official_job_url(webflow_row["id"]) if webflow_row else None,
                    "keka_jobdetail": keka_jobdetail_url(keka_id),
                    "keka_apply": keka_apply_url(keka_id),
                    "listing_feed": feed_url,
                    "keka_active_feed": keka_active_url,
                },
                "employment_labels": {
                    "official_listing_card": listing_type,
                    "official_detail_page": normalized_title(webflow_row.get("employement_type")) if webflow_row else None,
                    "keka_job_type_code": keka_row.get("jobType") if keka_row else None,
                    "keka_job_type_label": JOB_TYPE_LABELS.get(keka_row.get("jobType")) if keka_row else None,
                },
                "description_sections": sections,
                "description_links": [],
                "role_attachments": [],
                "source_dates": date_posted_raw,
                "source_ids": {
                    "official_webflow_uuid": webflow_row.get("id") if webflow_row else None,
                    "keka_job_id": keka_id,
                    "friendly_id": webflow_row.get("friendly_id") if webflow_row else None,
                },
                "salary_source": {
                    "keka_salary_range": keka_row.get("salaryRange") if keka_row else None,
                    "keka_salary_range_format": keka_row.get("salaryRangeFormat") if keka_row else None,
                },
                "workplace_evidence": {
                    "webflow_location_is_remote": (webflow_row.get("location") or {}).get("is_remote") if webflow_row else None,
                    "keka_job_locations": keka_row.get("jobLocations") if keka_row else None,
                },
            },
            title=title,
            url=job_url,
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method=description_method,
            description_url=description_url,
            locations=locations,
            department=department,
            team=normalized_title(keka_row.get("departmentName")) if keka_row else None,
            employment_type=employment_type,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
            date_updated=None,
            valid_through=None,
            salary=salary_from_row(keka_row),
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=keka_row.get("experience") if keka_row else None,
            employer=EMPLOYER,
            language="en",
        )
        job.description_text = description_text
        job.record_identity = record_identity
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def _keka_detail(self, row: dict, label: str):
        url = keka_jobdetail_url(row["id"])
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"SquadStack Keka {label} detail redirected away from its exact job route")
        parsed = parse_keka_detail(response.text, row, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "listing_url": OFFICIAL_FINAL,
            "listing_feed_url": LISTING_FEED,
            "official_detail_url_template": JOB_PAGE,
            "keka_origin": KEKA_ORIGIN,
            "membership_authority": (
                "SquadStack's own https://www.squadstack.ai/careers page fetches its complete "
                "https://careers.squadstack.com/webflow.php array; the cards and detail pages are rendered "
                "from that array. The company's Keka tenant active feed is an independent cross-check."
            ),
            "listing_filter_behavior": "All Teams / All Locations filters are client-side over the same array; no filter was applied.",
            "ui_pagination": "No pagination or load-more control; the script renders all rows at once, grouped by department.",
            "detail_method": "Full description HTML is inline in each webflow.php listing row; the official /job-description page renders that same field.",
            "request_scope": "Only SquadStack's own careers page/feed and the exact Keka tenant/job IDs published by it.",
        })
        try:
            initial_entry = await self._entry()
            initial_feed = await self._listing_feed("initial")
        except Exception as exc:
            self.result.errors.append(f"SquadStack official source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial_feed["count"]
        self.result.pages = 1
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing_feed"] = {
            key: value for key, value in initial_feed.items() if key != "rows"
        }
        self.result.metadata["initial_listing_ids"] = initial_feed["ids"]
        self.result.metadata["initial_listing_keka_ids"] = initial_feed["keka_ids"]
        self.result.errors.extend(initial_feed["issues"])
        if not initial_feed["rows"]:
            self.result.errors.append("squadstack_official_listing_returned_zero_jobs")

        origins = set()
        for row in initial_feed["rows"]:
            parts = urlsplit(row.get("url") or "")
            if parts.scheme == "https" and parts.hostname:
                origins.add(f"{parts.scheme}://{parts.netloc}")
        keka_board = None
        try:
            keka_board = await self._keka_board(origins)
        except Exception as exc:
            self.result.errors.append(f"SquadStack Keka cross-check failed: {type(exc).__name__}: {exc}")
        self.result.metadata["keka_board"] = (
            {key: value for key, value in keka_board.items() if key != "active"} if keka_board else None
        )
        if keka_board:
            active = keka_board["active"]
            self.result.metadata["initial_keka_active"] = {
                key: value for key, value in active.items() if key != "rows"
            }
            self.result.metadata["initial_keka_active_ids"] = active["ids"]
            self.result.errors.extend(active["issues"])
            webflow_keka_ids = {str(identity) for identity in initial_feed["keka_ids"]}
            keka_ids = {str(identity) for identity in active["ids"]}
            if webflow_keka_ids != keka_ids:
                self.result.errors.append(
                    "squadstack_membership_mismatch_between_official_webflow_feed_and_keka_active_feed: "
                    f"webflow_only={sorted(webflow_keka_ids - keka_ids)} "
                    f"keka_only={sorted(keka_ids - webflow_keka_ids)}"
                )

        keka_by_id = {str(row["id"]): row for row in (keka_board["active"]["rows"] if keka_board else [])}
        webflow_keka_ids = {str(keka_id_from_webflow_row(row)) for row in initial_feed["rows"]}
        keka_only_ids = [identity for identity in keka_by_id if identity not in webflow_keka_ids]

        jobs = []
        detail_parse_seconds = 0.0
        ordered_rows = ui_order(initial_feed["rows"], initial_entry["preferred_ids"])
        for webflow_row in ordered_rows:
            keka_id = keka_id_from_webflow_row(webflow_row)
            keka_row = keka_by_id.get(str(keka_id))
            if keka_row is not None:
                if normalized_title(webflow_row.get("name")) != normalized_title(keka_row.get("title")):
                    self.result.errors.append(f"squadstack_keka_title_mismatch:{webflow_row['id']}")
                if comparison_text(webflow_row.get("description")) != comparison_text(keka_row.get("description")):
                    self.result.errors.append(f"squadstack_keka_description_mismatch:{webflow_row['id']}")
                if not same_datetime(webflow_row.get("published_date"), keka_row.get("publishedOn")):
                    self.result.errors.append(f"squadstack_keka_date_mismatch:{webflow_row['id']}")
                webflow_locations = {
                    normalized_title(location.get("name"))
                    for location in (webflow_row.get("locations") or [])
                    if isinstance(location, dict)
                }
                keka_locations = {
                    normalized_title(location.get("name"))
                    for location in (keka_row.get("jobLocations") or [])
                    if isinstance(location, dict)
                }
                if webflow_locations != keka_locations:
                    self.result.errors.append(f"squadstack_keka_location_mismatch:{webflow_row['id']}")
                listing_type = normalized_title((webflow_row.get("type") or {}).get("name"))
                keka_type = JOB_TYPE_LABELS.get(keka_row.get("jobType"))
                if listing_type and keka_type and listing_type.lower() != keka_type.lower():
                    self.result.errors.append(f"squadstack_keka_job_type_mismatch:{webflow_row['id']}")
            started = time.perf_counter()
            job = self._make_job(
                webflow_row=webflow_row,
                keka_row=keka_row,
                feed_url=LISTING_FEED,
                keka_active_url=keka_board["active_url"] if keka_board else None,
                membership_source="official_webflow_feed" if keka_row is None else "official_webflow_feed+keka_active",
            )
            detail_parse_seconds += max(0.0, time.perf_counter() - started)
            jobs.append(job)

        for identity in keka_only_ids:
            keka_row = keka_by_id[identity]
            started = time.perf_counter()
            job = self._make_job(
                webflow_row=None,
                keka_row=keka_row,
                feed_url=LISTING_FEED,
                keka_active_url=keka_board["active_url"] if keka_board else None,
                membership_source="keka_active_only",
            )
            detail_parse_seconds += max(0.0, time.perf_counter() - started)
            jobs.append(job)

        if len(jobs) != initial_feed["count"]:
            self.result.errors.append(
                f"squadstack_retained_rows_do_not_match_official_listing_count: {len(jobs)} != {initial_feed['count']}"
            )

        representative_details = {}
        if keka_board and initial_feed["rows"]:
            for label, row in (("first", ordered_rows[0]), ("last", ordered_rows[-1])):
                keka_id = keka_id_from_webflow_row(row)
                keka_row = keka_by_id.get(str(keka_id))
                if keka_row is None:
                    self.result.errors.append(f"squadstack_representative_{label}_missing_from_keka_active_feed")
                    continue
                try:
                    detail = await self._keka_detail(keka_row, label)
                except Exception as exc:
                    self.result.errors.append(
                        f"SquadStack representative {label} Keka detail {keka_jobdetail_url(keka_id)}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                expected = next(job for job in jobs if job.job_id == str(row["id"]))
                if comparison_text(detail["description_html"]) != comparison_text(expected.description_html):
                    self.result.errors.append(f"squadstack_representative_{label}_detail_body_mismatch")
                representative_details[label] = {
                    "webflow_uuid": row["id"],
                    "keka_job_id": str(keka_id),
                    "title": detail["title"],
                    "url": detail["url"],
                    "status": detail["status"],
                    "seconds": detail["seconds"],
                    "response_sha256": detail["response_sha256"],
                    "response_bytes": detail["response_bytes"],
                    "description_normalized_chars": detail["description_normalized_chars"],
                    "description_fnv1a64": detail["description_fnv1a64"],
                    "description_alphanumeric_fnv1a64": detail["description_alphanumeric_fnv1a64"],
                    "headings": detail["headings"],
                    "apply_control": detail["apply_control"],
                    "matches_stored_description": True,
                }

        final_feed = None
        final_keka = None
        final_entry = None
        try:
            final_entry = await self._entry()
            final_feed = await self._listing_feed("final")
            if keka_board:
                final_active_started = time.perf_counter()
                response = await self.http.get(keka_board["active_url"], headers={"Referer": KEKA_CAREERS})
                final_active_seconds = max(0.0, time.perf_counter() - final_active_started)
                if str(response.url) != keka_board["active_url"]:
                    raise ValueError("SquadStack final Keka active feed redirected away from its inspected URL")
                final_keka = parse_keka_active(response.json())
                final_keka.update(
                    status=response.status_code,
                    seconds=final_active_seconds,
                    response_sha256=sha256(response.content).hexdigest(),
                    response_bytes=len(response.content),
                )
        except Exception as exc:
            self.result.errors.append(f"SquadStack final source recheck failed: {type(exc).__name__}: {exc}")

        if final_feed is None or final_entry is None or (keka_board and final_keka is None):
            membership_verified = False
            self.result.errors.append("squadstack_final_membership_recheck_incomplete")
        else:
            final_ids = final_feed["ids"]
            final_keka_ids = final_keka["ids"] if final_keka else []
            initial_by_id = {str(row["id"]): row for row in initial_feed["rows"]}
            final_by_id = {str(row["id"]): row for row in final_feed["rows"]}
            changed_fields = {
                identity: sorted(
                    key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                    if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                )
                for identity in set(initial_by_id) & set(final_by_id)
                if initial_by_id[identity] != final_by_id[identity]
            }
            same_ids = initial_feed["ids"] == final_ids
            same_keka_ids = (not keka_board) or (keka_board["active"]["ids"] == final_keka_ids)
            same_entry = (
                initial_entry["listing_feed_url"] == final_entry["listing_feed_url"] == LISTING_FEED
                and initial_entry["preferred_ids"] == final_entry["preferred_ids"]
            )
            membership_verified = bool(
                same_ids and same_keka_ids and same_entry and not changed_fields
                and not final_feed["issues"] and not (final_keka and final_keka["issues"])
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing_feed"] = {
                key: value for key, value in final_feed.items() if key != "rows"
            }
            self.result.metadata["final_listing_ids"] = final_ids
            self.result.metadata["final_listing_keka_ids"] = final_feed["keka_ids"]
            if final_keka:
                self.result.metadata["final_keka_active"] = {
                    key: value for key, value in final_keka.items() if key != "rows"
                }
                self.result.metadata["final_keka_active_ids"] = final_keka_ids
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_feed["ids"],
                "final_ids": final_ids,
                "initial_keka_ids": initial_feed["keka_ids"],
                "final_keka_ids": final_feed["keka_ids"],
                "initial_keka_active_ids": keka_board["active"]["ids"] if keka_board else None,
                "final_keka_active_ids": final_keka_ids if final_keka else None,
                "official_listing_count": initial_feed["count"],
                "official_listing_final_count": final_feed["count"],
                "keka_active_count": keka_board["active"]["count"] if keka_board else None,
                "keka_active_final_count": final_keka["count"] if final_keka else None,
                "same_official_ids_and_order": same_ids,
                "same_keka_active_ids": same_keka_ids,
                "official_entry_still_same_feed": same_entry,
                "changed_fields_by_uuid": changed_fields,
                "new_ids": [identity for identity in final_ids if identity not in initial_feed["ids"]],
                "removed_ids": [identity for identity in initial_feed["ids"] if identity not in final_ids],
                "complete": membership_verified,
            }
            if not same_ids:
                self.result.errors.append("squadstack_official_listing_membership_changed_during_collection")
            if not same_keka_ids:
                self.result.errors.append("squadstack_keka_active_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("squadstack_source_fields_changed_during_collection")
            if not same_entry:
                self.result.errors.append("squadstack_official_entry_or_feed_changed_during_collection")

        self.result.jobs = jobs
        for job in self.result.jobs:
            self.result.validate_job(job)
        self.result.timings["detail_parse_seconds_sum"] = detail_parse_seconds
        self.result.timings["detail_phase_seconds"] = detail_parse_seconds
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.metadata["representative_detail_audits"] = representative_details
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["visible_ui_order_ids"] = [str(row["id"]) for row in ordered_rows]
        self.result.metadata["keka_only_job_ids"] = keka_only_ids
        self.result.metadata["current_native_membership_reconciled"] = membership_verified
        self.result.metadata["source_collection_wall_seconds"] = self.result.timings["source_collection_wall_seconds"]
        self.result.listing_complete = bool(
            membership_verified and len(jobs) == initial_feed["count"] and not self.result.errors
        )
        return self.result
