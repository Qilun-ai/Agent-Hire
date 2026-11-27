import csv
import json
from importlib.resources import files
from pathlib import Path

from .models import Company, Source, slug


def load_registry(path: Path | None = None) -> list[Company]:
    if path:
        data = json.loads(path.read_text())
        return [Company(**{**row, "sources": [Source(**s) for s in row.get("sources", [])]}) for row in data]
    directory = files("career_scraper")
    with directory.joinpath("career_sites.tsv").open() as handle:
        urls = dict(csv.reader(handle, delimiter="\t"))
    names = directory.joinpath("requested_companies.txt").read_text().splitlines()
    if set(names) != set(urls):
        raise ValueError(f"Registry/list mismatch: {set(names) ^ set(urls)}")
    companies = []
    for name in names:
        note = ""
        scope = "company"
        if name == "Data Insights":
            note = "Skipped at the user's explicit request on 2026-11-05."
        if name in {"Oracle GBU", "SAP Labs", "Mindtree", "L&T Infotech", "Walmart Labs",
                    "Walmart Global Tech India", "Samsung R&D", "Samsung Semiconductor India Research",
                    "AppDynamics", "Clumio", "Cradlepoint", "Loom", "SendGrid", "Slack", "VMware",
                    "Steradian Semiconductors Pvt Ltd", "Bosch Global Software Technologies", "Alphawave Semi"}:
            scope = "division"
            note = "Division/legacy name: parent board needs explicit division matching; parent-wide results are not verified company coverage."
        aliases = {"https://jobs.ashbyhq.com/Redis": "https://jobs.ashbyhq.com/redis"} if name == "Redis" else {}
        companies.append(Company(name, slug(name), urls[name], notes=note, scope=scope, enabled=name != "Data Insights", source_aliases=aliases))
    return companies
