"""Write the list of scraped company career sites into README.md (between the companies markers).

Usage: python3 scripts/readme_companies.py
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "services" / "careers-scraper" / "companies.json"
README = ROOT / "README.md"
START = "<!-- companies:start -->"
END = "<!-- companies:end -->"
COLUMNS = 3


def main() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    companies = data["companies"] if isinstance(data, dict) else data
    verified = sorted(
        (c for c in companies if c.get("status") == "verified" and c.get("careers_url")),
        key=lambda c: c["company"].lower(),
    )
    cells = [f"[{c['company']}]({c['careers_url']})" for c in verified]
    while len(cells) % COLUMNS:
        cells.append("")
    rows = [cells[i : i + COLUMNS] for i in range(0, len(cells), COLUMNS)]
    table = "\n".join(
        [
            "<details>",
            f"<summary><b>{len(verified)} company career sites</b> (click to expand)</summary>",
            "",
            "| | | |",
            "|---|---|---|",
            *("| " + " | ".join(row) + " |" for row in rows),
            "",
            "</details>",
        ]
    )
    text = README.read_text(encoding="utf-8")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    README.write_text(f"{head}{START}\n{table}\n{END}{tail}", encoding="utf-8")
    print(f"README.md: {len(verified)} companies")


if __name__ == "__main__":
    main()
