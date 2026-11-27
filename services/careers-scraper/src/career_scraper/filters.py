"""Per-company filter and sort schemas for the careers portals.

Each company's portal exposes its own search controls: keyword boxes, facet
lists (location, department, job family, time type), sort menus and, for some
portals, a native query API. This module keeps that knowledge per company in
``companies/<slug>/filters.json`` and provides:

* a validated schema (:class:`FilterSchema`) that the HTML form/API can be
  documented and driven from,
* family defaults for the shared ATS engines (Workday, Eightfold PCSX,
  SmartRecruiters, Oracle, Phenom, Workable, Greenhouse, Lever, Ashby, TikTok),
* a local query engine (:func:`apply_query`) that filters any collected job set
  on the canonical fields when the site has no usable native parameters,
* Markdown rendering for ``reports/company-filters.md``.

Canonical fields are stable across companies; ``native_param`` records the
company's own parameter name so a native query can be built where supported.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]

CANONICAL_FIELDS = (
    "keyword", "location", "country", "department", "team", "job_family",
    "employment_type", "workplace_type", "seniority", "business_unit",
    "date_posted",
)

SORT_LABELS = {
    "relevance": "Relevance",
    "recent": "Most recent",
    "oldest": "Oldest",
    "title": "Title A-Z",
}


@dataclass
class FilterField:
    key: str
    label: str
    kind: str = "multi"          # multi | text | enum | date | bool
    values: list[str] = field(default_factory=list)
    native_param: str | None = None
    note: str = ""

    def validate(self) -> "FilterField":
        self.key = self.key.strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {"category": "department", "job_category": "department",
                   "jobtype": "employment_type", "job_type": "employment_type",
                   "type": "employment_type", "remote": "workplace_type",
                   "city": "location", "location_text": "location",
                   "experiencelevel": "seniority", "experience_level": "seniority",
                   "businessunit": "business_unit", "jobfamily": "job_family",
                   "job_family_group": "job_family"}
        self.key = aliases.get(self.key, self.key)
        if self.key not in CANONICAL_FIELDS:
            raise ValueError(f"unknown canonical filter {self.key!r}")
        aliases = {"single": "enum", "select": "enum", "multiselect": "multi",
                   "multi_select": "multi", "date_range": "date", "range": "text"}
        self.kind = aliases.get(self.kind, self.kind)
        if self.kind not in {"multi", "text", "enum", "date", "bool"}:
            raise ValueError(f"bad filter kind {self.kind!r}")
        self.values = [str(v) for v in self.values if str(v).strip()][:50]
        return self


@dataclass
class SortOption:
    value: str
    label: str = ""
    native_param: str | None = None


@dataclass
class FilterSchema:
    company: str
    slug: str
    portal: str = ""
    listing_url: str = ""
    family: str = "custom"
    keyword: bool = True
    keyword_param: str | None = None
    fields: list[FilterField] = field(default_factory=list)
    sorts: list[SortOption] = field(default_factory=list)
    default_sort: str | None = None
    native_query: bool = False
    local_query: bool = True
    notes: str = ""
    evidence: list[str] = field(default_factory=list)
    verified: bool = False

    def field_map(self) -> dict[str, FilterField]:
        return {item.key: item for item in self.fields}

    def validate(self) -> "FilterSchema":
        seen = set()
        for item in self.fields:
            item.validate()
            if item.key in seen:
                raise ValueError(f"{self.slug}: duplicate filter {item.key!r}")
            seen.add(item.key)
        if self.keyword and not self.keyword_param and not self.native_query:
            # Keyword is applied locally; the site parameter is unknown.
            self.keyword_param = None
        return self

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fields"] = [asdict(item) for item in self.fields]
        data["sorts"] = [asdict(item) for item in self.sorts]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilterSchema":
        return cls(
            company=data["company"], slug=data["slug"], portal=data.get("portal", ""),
            listing_url=data.get("listing_url", ""), family=data.get("family", "custom"),
            keyword=bool(data.get("keyword", True)),
            keyword_param=data.get("keyword_param"),
            fields=[FilterField(**item) for item in data.get("fields", [])],
            sorts=[SortOption(**item) for item in data.get("sorts", [])],
            default_sort=data.get("default_sort"),
            native_query=bool(data.get("native_query", False)),
            local_query=bool(data.get("local_query", True)),
            notes=data.get("notes", ""), evidence=list(data.get("evidence", [])),
            verified=bool(data.get("verified", False)),
        ).validate()


# --------------------------------------------------------------------------- families

def _f(key, label, values=(), param=None, kind="multi", note=""):
    return FilterField(key=key, label=label, kind=kind, values=list(values),
                       native_param=param, note=note)


FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "workday": {
        "keyword_param": "searchText",
        "native": True,
        "fields": [
            _f("location", "Location", param="appliedFacets[locations]"),
            _f("department", "Department", param="appliedFacets[jobFamilyGroup]"),
            _f("job_family", "Job family", param="appliedFacets[jobFamily]"),
            _f("employment_type", "Time type", param="appliedFacets[timeType]"),
            _f("workplace_type", "Workplace type", param="appliedFacets[workplaceType]"),
            _f("country", "Country", param="appliedFacets[locationCountry]"),
        ],
        "sorts": [SortOption("relevance", "Relevance", "sortBy=relevance"),
                  SortOption("recent", "Most recent", "sortBy=postedOn")],
        "default_sort": "relevance",
        "note": "Workday CXS search accepts appliedFacets + searchText + sortBy.",
    },
    "eightfold": {
        "keyword_param": "query",
        "native": True,
        "fields": [
            _f("location", "Location", param="location"),
            _f("department", "Department", param="department"),
            _f("workplace_type", "Workplace", param="work_location_option"),
            _f("country", "Country", param="country"),
        ],
        "sorts": [SortOption("recent", "Most recent", "sort_by=timestamp"),
                  SortOption("relevance", "Relevance", "sort_by=relevance")],
        "default_sort": "relevance",
        "note": "Eightfold PCSX search accepts query/location/department/sort_by/start.",
    },
    "smartrecruiters": {
        "keyword_param": "q",
        "native": True,
        "fields": [_f("location", "Location", param="location"),
                   _f("department", "Department", param="department"),
                   _f("employment_type", "Job type", param="typeOfEmployment")],
        "sorts": [SortOption("recent", "Most recent", "sort=recent")],
        "default_sort": "recent",
        "note": "SmartRecruiters postings API supports q/location and client-side facets.",
    },
    "oracle": {
        "keyword_param": "keyword",
        "native": True,
        "fields": [_f("location", "Location", param="location"),
                   _f("department", "Organization", param="department"),
                   _f("job_family", "Job family", param="jobFamily"),
                   _f("workplace_type", "Workplace", param="workplaceType")],
        "sorts": [SortOption("recent", "Most recent", "sortBy=POSTING_DATES_DESC")],
        "default_sort": "recent",
        "note": "Oracle Candidate Experience REST supports keyword/location/flexFields.",
    },
    "phenom": {
        "keyword_param": "keywords",
        "native": True,
        "fields": [_f("location", "Location", param="location"),
                   _f("country", "Country", param="country"),
                   _f("department", "Category", param="category"),
                   _f("employment_type", "Type", param="type")],
        "sorts": [SortOption("recent", "Most recent", "sortBy=date")],
        "default_sort": "recent",
        "note": "Phenom widgets accept keywords/country/category/type.",
    },
    "workable": {
        "keyword_param": "query",
        "native": True,
        "fields": [_f("location", "Location", param="location"),
                   _f("department", "Department", param="department"),
                   _f("workplace_type", "Workplace", param="workplace"),
                   _f("employment_type", "Work type", param="worktype")],
        "sorts": [],
        "note": "Workable v3 jobs body carries query/department/location/workplace/worktype.",
    },
    "tiktok": {
        "keyword_param": "keyword",
        "native": True,
        "fields": [_f("location", "Location", param="location_id_list"),
                   _f("department", "Category", param="job_category_id_list"),
                   _f("employment_type", "Recruitment type", param="recruitment_id_list")],
        "sorts": [],
        "note": "TikTok supplier search carries keyword and id lists.",
    },
    "greenhouse": {
        "keyword_param": None, "native": False,
        "fields": [_f("department", "Department"), _f("location", "Location")],
        "sorts": [], "note": "Board API returns the whole set; filters are local.",
    },
    "lever": {
        "keyword_param": None, "native": False,
        "fields": [_f("team", "Team"), _f("location", "Location"),
                   _f("employment_type", "Commitment")],
        "sorts": [], "note": "Postings API returns the whole set; filters are local.",
    },
    "ashby": {
        "keyword_param": None, "native": False,
        "fields": [_f("department", "Department"), _f("location", "Location"),
                   _f("employment_type", "Employment type")],
        "sorts": [], "note": "Job board API returns the whole set; filters are local.",
    },
    "custom": {
        "keyword_param": None, "native": False,
        "fields": [_f("location", "Location"), _f("department", "Department"),
                   _f("employment_type", "Employment type"),
                   _f("workplace_type", "Workplace")],
        "sorts": [], "note": "Custom company flow; filters are applied locally.",
    },
}


def default_schema(company: str, slug: str, *, portal: str = "", family: str = "custom",
                   listing_url: str = "") -> FilterSchema:
    spec = FAMILY_DEFAULTS.get(family, FAMILY_DEFAULTS["custom"])
    fields = [
        FilterField(**{**asdict(item), "values": []})
        for item in spec["fields"]
    ]
    return FilterSchema(
        company=company, slug=slug, portal=portal, listing_url=listing_url,
        family=family, keyword=True,  # every flow offers at least local keyword search
        keyword_param=spec.get("keyword_param"),
        fields=fields,
        sorts=[SortOption(**asdict(item)) for item in spec.get("sorts", [])],
        default_sort=spec.get("default_sort"),
        native_query=bool(spec.get("native")),
        local_query=True,
        notes=spec.get("note", ""),
    ).validate()


# --------------------------------------------------------------------------- storage

def schema_path(slug: str, root: Path | None = None) -> Path:
    return (root or ROOT) / "companies" / slug / "filters.json"


def load_schema(slug: str, root: Path | None = None) -> FilterSchema | None:
    path = schema_path(slug, root)
    if not path.is_file():
        return None
    return FilterSchema.from_dict(json.loads(path.read_text()))


def save_schema(schema: FilterSchema, root: Path | None = None) -> Path:
    schema.validate()
    path = schema_path(schema.slug, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return path


def load_all(root: Path | None = None, *, strict: bool = False) -> dict[str, FilterSchema]:
    base = root or ROOT
    schemas: dict[str, FilterSchema] = {}
    errors: list[str] = []
    for path in sorted((base / "companies").glob("*/filters.json")):
        try:
            schema = FilterSchema.from_dict(json.loads(path.read_text()))
        except (ValueError, KeyError) as exc:
            errors.append(f"{path}: {exc}")
            if strict:
                raise ValueError(errors[-1]) from exc
            continue
        schemas[schema.slug] = schema
    if errors:
        import sys
        print(f"skipped {len(errors)} invalid filter schemas", file=sys.stderr)
        for message in errors[:10]:
            print("  " + message, file=sys.stderr)
    return schemas


# --------------------------------------------------------------------------- query

@dataclass
class JobQuery:
    keyword: str | None = None
    filters: dict[str, list[str]] = field(default_factory=dict)
    sort: str | None = None
    since: str | None = None
    limit: int | None = None

    def is_empty(self) -> bool:
        return not (self.keyword or self.filters or self.since or self.sort)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(v) for v in value)
    return str(value)


def _field_text(job: Any, key: str) -> str:
    attr = {
        "location": "locations", "country": "locations",
        "department": "department", "team": "team",
        "job_family": "department", "employment_type": "employment_type",
        "workplace_type": "workplace_type", "seniority": "title",
        "business_unit": "department", "date_posted": "date_posted",
    }.get(key, key)
    if isinstance(job, dict):
        return _text(job.get(attr))
    return _text(getattr(job, attr, None))


CITY_ALIASES = {
    "bengaluru": "bangalore", "gurugram": "gurgaon", "bombay": "mumbai",
    "calcutta": "kolkata", "madras": "chennai", "mysuru": "mysore",
    "prayagraj": "allahabad", "vadodara": "baroda", "trivandrum": "thiruvananthapuram",
}
CITY_COUNTRY = {
    "bangalore": "india", "bengaluru": "india", "mumbai": "india", "pune": "india",
    "hyderabad": "india", "chennai": "india", "delhi": "india", "gurgaon": "india",
    "gurugram": "india", "noida": "india", "kolkata": "india", "ahmedabad": "india",
    "jaipur": "india", "london": "united kingdom", "new york": "united states",
    "san francisco": "united states", "seattle": "united states", "austin": "united states",
    "tokyo": "japan", "singapore": "singapore", "dublin": "ireland", "berlin": "germany",
    "paris": "france", "sydney": "australia", "toronto": "canada", "tel aviv": "israel",
    "warsaw": "poland", "madrid": "spain", "milan": "italy", "zurich": "switzerland",
    "stockholm": "sweden", "copenhagen": "denmark", "amsterdam": "netherlands",
    "sao paulo": "brazil", "mexico city": "mexico", "dubai": "united arab emirates",
    "shanghai": "china", "beijing": "china", "seoul": "south korea",
}
COUNTRY_CODES = {
    "india": "in", "ind": "in", "united states": "us", "united states of america": "us", "usa": "us",
    "united kingdom": "uk", "great britain": "uk", "england": "uk",
    "united arab emirates": "ae", "singapore": "sg", "australia": "au",
    "canada": "ca", "germany": "de", "france": "fr", "japan": "jp",
    "china": "cn", "brazil": "br", "mexico": "mx", "netherlands": "nl",
    "ireland": "ie", "israel": "il", "poland": "pl", "spain": "es",
    "italy": "it", "switzerland": "ch", "sweden": "se", "denmark": "dk",
}


def _normalize_location(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    for source, target in CITY_ALIASES.items():
        text = text.replace(source, target)
    return text


def _country_matches(haystack: str, values: list[str]) -> bool:
    text = _normalize_location(haystack)
    for wanted in values:
        name = _normalize_location(wanted)
        code = COUNTRY_CODES.get(name, name if len(name) == 2 else None)
        if name and name in text:
            return True
        # Wanted country -> match a known city of that country in the record.
        canonical = name if name in set(CITY_COUNTRY.values()) else None
        if canonical is None and name in COUNTRY_CODES:
            canonical = next((country for country in set(CITY_COUNTRY.values())
                              if COUNTRY_CODES.get(country) == COUNTRY_CODES[name]), None)
        if canonical:
            for city, mapped in CITY_COUNTRY.items():
                if mapped == canonical and city in text:
                    return True
        if code and (text == code or text.startswith(code + ",") or text.endswith(", " + code)
                     or text.endswith(", " + code + " ")):
            return True
    return False


def _matches(job: Any, key: str, wanted: Iterable[str]) -> bool:
    raw = _field_text(job, key)
    values = [str(value).strip() for value in wanted if str(value).strip()]
    if not values:
        return True
    if key in {"country", "location"}:
        locations = (job.get("locations") if isinstance(job, dict)
                     else getattr(job, "locations", None)) or []
        texts = [str(location.get("location") or "") if isinstance(location, dict)
                 else str(location or "") for location in locations]
        if not texts:
            texts = [raw]
        if key == "country":
            return any(_country_matches(text, values) for text in texts)
        return any(
            _normalize_location(value) in _normalize_location(text)
            or _country_matches(text, [value])
            for text in texts for value in values
        )
    haystack = _normalize_location(raw)
    return any(_normalize_location(value) in haystack for value in values)


def apply_query(jobs: Iterable[Any], query: JobQuery) -> list[Any]:
    """Filter/sort an already-collected job list on the canonical fields."""
    selected = list(jobs)
    if query.keyword:
        words = [word for word in re.split(r"\s+", query.keyword.strip()) if word]
        needle = query.keyword.casefold()
        selected = [
            job for job in selected
            if needle in _field_text(job, "title").casefold()
            or needle in (_text(job.get("description_text")) if isinstance(job, dict)
                          else _text(getattr(job, "description_text", ""))).casefold()
            or all(word.casefold() in _text(job).casefold() for word in words)
        ]
    for key, wanted in query.filters.items():
        selected = [job for job in selected if _matches(job, key, wanted)]
    if query.since:
        selected = [
            job for job in selected
            if str((job.get("date_posted") if isinstance(job, dict)
                    else getattr(job, "date_posted", "")) or "") >= query.since
        ]
    if query.sort:
        if query.sort in {"recent", "oldest"}:
            reverse = query.sort == "recent"
            selected.sort(
                key=lambda job: str((job.get("date_posted") if isinstance(job, dict)
                                     else getattr(job, "date_posted", "")) or ""),
                reverse=reverse,
            )
        elif query.sort == "title":
            selected.sort(key=lambda job: _field_text(job, "title").casefold())
    if query.limit:
        selected = selected[: query.limit]
    return selected


def native_query(schema: FilterSchema, query: JobQuery) -> dict[str, Any] | None:
    """Build the portal's own request parameters where the family supports it."""
    if not schema.native_query:
        return None
    params: dict[str, Any] = {}
    if query.keyword and schema.keyword_param:
        params[schema.keyword_param] = query.keyword
    fields = schema.field_map()
    for key, wanted in query.filters.items():
        item = fields.get(key)
        if not item or not item.native_param or not wanted:
            continue
        param = item.native_param
        if schema.family == "workday":
            facet = param.split("[", 1)[-1].rstrip("]")
            params.setdefault("appliedFacets", {})[facet] = list(wanted)
        elif schema.family in {"eightfold", "tiktok"} and item.key in {"location", "department"}:
            params[param] = list(wanted)
        elif len(wanted) == 1:
            params[param] = wanted[0]
        else:
            params[param] = list(wanted)
    if query.sort:
        for option in schema.sorts:
            if option.value == query.sort and option.native_param:
                if "=" in option.native_param:
                    name, value = option.native_param.split("=", 1)
                    params[name] = value
                break
    return params or None


# --------------------------------------------------------------------------- values

def observed_values(jobs: Iterable[Any]) -> dict[str, list[str]]:
    """Collect real filter values from a company's stored jobs."""
    values: dict[str, list[str]] = {key: [] for key in CANONICAL_FIELDS}
    seen: dict[str, set[str]] = {key: set() for key in CANONICAL_FIELDS}

    def add(key: str, value: str):
        value = re.sub(r"\s+", " ", str(value)).strip()
        if not value or len(value) > 120 or value in seen[key]:
            return
        seen[key].add(value)
        values[key].append(value)

    for job in jobs:
        locations = job.get("locations") if isinstance(job, dict) else getattr(job, "locations", [])
        for location in locations or []:
            text = location.get("location") if isinstance(location, dict) else location
            if text:
                add("location", str(text))
        for key in ("department", "employment_type", "workplace_type"):
            value = job.get(key) if isinstance(job, dict) else getattr(job, key, None)
            text = value.get("name") if isinstance(value, dict) else _text(value)
            if text:
                add(key, text)
        date_value = job.get("date_posted") if isinstance(job, dict) else getattr(job, "date_posted", None)
        if date_value:
            add("date_posted", str(date_value)[:10])
    for key in values:
        values[key].sort(key=str.casefold)
        values[key] = values[key][:25]
    values["date_posted"] = values["date_posted"][:5]
    return values


def enrich_with_values(schema: FilterSchema, values: dict[str, list[str]]) -> FilterSchema:
    for item in schema.fields:
        if values.get(item.key) and not item.values:
            item.values = values[item.key]
    return schema


# --------------------------------------------------------------------------- markdown

def _value_cell(values: list[str]) -> str:
    if not values:
        return "—"
    shown = ", ".join(value.replace("|", "/") for value in values[:8])
    return shown + (f" (+{len(values) - 8})" if len(values) > 8 else "")


def render_company(schema: FilterSchema) -> str:
    lines = [f"### {schema.company}", ""]
    lines.append(f"- Portal: {schema.portal or schema.listing_url or '—'}")
    lines.append(f"- Schema: {'verified against the live portal' if schema.verified else 'seeded (not live-verified)'}")
    lines.append(f"- Engine/family: `{schema.family}`")
    lines.append("- Keyword search: "
                 + (f"native `{schema.keyword_param}`" if schema.keyword_param
                    else "local (title + description)")
                 if schema.keyword else "- Keyword search: not exposed")
    lines.append("- Query execution: "
                 + ("native portal parameters" if schema.native_query
                    else "local on collected records"))
    if schema.default_sort:
        lines.append(f"- Default sort: `{schema.default_sort}`")
    if schema.notes:
        lines.append(f"- Notes: {schema.notes}")
    lines.append("")
    if schema.fields:
        lines += ["| Filter | Type | Values | Site parameter | Mode |",
                  "|---|---|---|---|---|"]
        for item in schema.fields:
            mode = "native" if item.native_param and schema.native_query else "local"
            lines.append(
                f"| {item.label} (`{item.key}`) | {item.kind} | {_value_cell(item.values)} | "
                f"`{item.native_param}` | {mode} |" if item.native_param else
                f"| {item.label} (`{item.key}`) | {item.kind} | {_value_cell(item.values)} | — | local |"
            )
    else:
        lines.append("_No structured filters exposed; keyword search only._")
    if schema.sorts:
        lines += ["", "Sort options: " + ", ".join(
            f"`{option.value}`" + (f" ({option.native_param})" if option.native_param else "")
            for option in schema.sorts
        )]
    if schema.evidence:
        lines += ["", "Evidence: " + ", ".join(f"`{item}`" for item in schema.evidence)]
    lines += ["", f"Query: `python3 scripts/company_filters.py query --company {schema.slug} "
                  f"--keyword <text> --filter <key>=<value>`", ""]
    return "\n".join(lines)


def render_markdown(schemas: dict[str, FilterSchema]) -> str:
    families: dict[str, list[FilterSchema]] = {}
    for schema in schemas.values():
        families.setdefault(schema.family, []).append(schema)
    lines = [
        "# Company careers portals — filters, sorting and keyword search",
        "",
        "Per-company filter schema used by `career_scraper.filters`. Each section lists the",
        "portal's own controls, the parameter name the site uses (when observed), whether the",
        "scraper can drive it natively or applies it locally, and the values observed in the",
        "company's collected records.",
        "",
        "## How to use",
        "",
        "```bash",
        "# what a company exposes",
        "python3 scripts/company_filters.py list --company stripe",
        "",
        "# build the native query the portal itself would send",
        "python3 scripts/company_filters.py query --company stripe --keyword engineer \\",
        "    --filter location=India --sort recent",
        "",
        "# filter an existing snapshot locally (no re-scrape)",
        "python3 scripts/company_filters.py apply --company stripe \\",
        "    --input output/reviewed-stripe-*/jobs.jsonl --keyword engineer \\",
        "    --filter department=Engineering --sort recent --out tmp/stripe-engineer.jsonl",
        "```",
        "",
        "`native` means the site's own search API accepts the parameter and the adapter can",
        "send it. `local` means the snapshot is filtered on the canonical fields after",
        "collection; membership totals stay untouched and the filter is recorded in the output.",
        "",
        "## Overview",
        "",
        "| Companies | Engine | Native query | Keyword |",
        "|---|---|---|---|",
    ]
    for family in sorted(families, key=lambda name: (-len(families[name]), name)):
        group = families[family]
        native = sum(1 for schema in group if schema.native_query)
        keyword = sum(1 for schema in group if schema.keyword)
        lines.append(f"| {len(group)} | `{family}` | {native} | {keyword} |")
    lines += ["", "---", ""]
    for family in sorted(families, key=lambda name: (-len(families[name]), name)):
        lines += [f"## {family}", ""]
        for schema in sorted(families[family], key=lambda item: item.company.casefold()):
            lines.append(render_company(schema))
    return "\n".join(lines) + "\n"
