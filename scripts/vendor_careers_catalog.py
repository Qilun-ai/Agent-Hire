#!/usr/bin/env python3
"""Write services/careers-scraper/companies.json from a company-careers-scraper checkout.

Keeps each company's review status and its recorded scrape runtime (seconds), which the app uses
to group companies into speed categories until it has measured its own runs.
"""
import json
import sys
from pathlib import Path

def unavailable_reason(row: dict) -> tuple[str | None, str | None]:
    """(group, short reason) for companies that are not scraped."""
    status, text = row.get("status"), (row.get("reason") or "")
    low = text.lower()
    if status == "verified":
        return None, None
    if status == "skipped":
        return "skipped", "Skipped at your request"
    # Reviewed by hand where the ledger wording trips the keyword rules below.
    overrides = {"Blinkit": "no_listing", "SWYM Corporation": "no_listing", "BYJU'S": "no_listing"}
    if row["company"] in overrides:
        return overrides[row["company"]], "No public job listing on the official careers site"
    if "absorbed" in low or "cisco legal employer" in low or row["company"] in {"AppDynamics", "Cradlepoint"}:
        return "merged", "Merged into its parent company; no separate listings"
    if any(k in low for k in ("stealth browser", "akamai", "http 503", "challenge", "service temporarily unavailable")):
        return "blocked", "Site blocks automated access or is down; needs a fresh browser run"
    if "reconciles" in low and "26,392" in low:
        return "verification", "Collected, but the counts did not reconcile during verification"
    if any(k in low for k in ("404", "no careers", "mailto", "email-only", "marketing", "no job", "shutdown", "redirect",
                              "publishes no", "0 job", "no open", "total=0", "401", "dns", "times out", "casino")):
        return "no_listing", "No public job listing on the official careers site"
    return "review", "Waiting for review"


home, out = Path(sys.argv[1]), Path(sys.argv[2])
rows = json.loads((home / "reports" / "company-review-status.json").read_text())["companies"]
companies = []
for r in rows:
    runtime = r.get("runtime_seconds")
    companies.append({
        "company": r["company"], "slug": r["slug"], "careers_url": r.get("careers_url"),
        "status": r.get("status"),
        "recorded_seconds": round(float(runtime), 1) if isinstance(runtime, (int, float)) else None,
        "recorded_jobs": r.get("verified_descriptions"),
        "unavailable_group": unavailable_reason(r)[0],
        "unavailable_reason": unavailable_reason(r)[1],
        "review_note": (r.get("reason") or "")[:400] if r.get("status") != "verified" else None,
    })
out.write_text(json.dumps({"source": "company-careers-scraper reports/company-review-status.json",
                           "companies": companies}, indent=1))
print(f"{len(companies)} companies, {sum(1 for c in companies if c['recorded_seconds'] is not None)} with timings")
