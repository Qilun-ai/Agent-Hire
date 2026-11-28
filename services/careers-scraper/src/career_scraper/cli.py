from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

from .adapters import ADAPTERS
from .adapters.base import Options
from .company_flows import COMPANY_ADAPTERS, COMPANY_REVIEW_BLOCKS
from .discovery import Discovery, detect, discover
from .http import HTTP
from .models import Result, Source, now
from .registry import load_registry
from .storage import Store, atomic_json, output_lock
from .timing import elapsed, format_duration


def parser():
    p = argparse.ArgumentParser(description="Collect jobs starting from each company's official careers website.")
    p.add_argument("--registry", type=Path, help="Optional custom JSON registry; sources are still discovered from official sites")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List every requested company and official career entry point")
    for command in ("discover", "scrape"):
        q = sub.add_parser(command)
        q.add_argument("--company", action="append", help="Exact company name or slug; repeat for multiple companies")
        q.add_argument("--all", action="store_true", help="Process all 279 requested entries")
        q.add_argument("--output", type=Path, default=Path("output/latest"))
        q.add_argument("--concurrency", type=int, default=4)
        q.add_argument("--delay", type=float, default=.3, help="Minimum seconds between requests to one host")
        q.add_argument("--timeout", type=float, default=35)
        q.add_argument("--max-response-mb", type=int, default=64, help="Maximum decompressed response size; oversized responses fail explicitly")
        q.add_argument("--discovery-pages", type=int, default=16)
        q.add_argument("--browser", action="store_true", help="Render JS-dependent career pages using optional Playwright")
        q.add_argument("--resume", action="store_true", help="Resume an interrupted snapshot in this output directory")
        if command == "scrape":
            q.add_argument("--max-jobs", type=int, help="Per-source sample cap; results are explicitly marked sampled")
            q.add_argument("--max-pages", type=int, default=10000, help="Explicit pagination safety budget; hitting it fails completeness")
            q.add_argument("--detail-concurrency", type=int, default=2,
                           help="Concurrent job details per source (default2 to bound memory)")
            q.add_argument("--discovery-dir", type=Path, help="Reuse recorded official-site discovery evidence from this directory")
            q.add_argument("--retry-unavailable", action="store_true", help="On resume, recheck descriptions already confirmed absent from their source")
            q.add_argument("--no-job-export", action="store_true", help="Save SQLite and coverage now; defer large JSONL/CSV regeneration until `careers report`")
    q = sub.add_parser("report", help="Regenerate JSONL, CSV and coverage reports from a run database")
    q.add_argument("--output", type=Path, default=Path("output/latest"))
    return p


def select(companies, args):
    if args.all:
        return companies
    if not args.company:
        raise ValueError("Choose --all or at least one --company")
    by_name = {key.lower(): c for c in companies for key in (c.name, c.slug)}
    unknown = [name for name in args.company if name.lower() not in by_name]
    if unknown:
        raise ValueError(f"Unknown companies: {unknown}. Use `careers list`.")
    return list({by_name[n.lower()].slug: by_name[n.lower()] for n in args.company}.values())


async def execute(args):
    run_started = time.perf_counter()
    run_started_at = now()
    companies = select(load_registry(args.registry), args)
    args.output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.concurrency)
    store = Store(args.output) if args.command == "scrape" else None
    last_progress = {}
    attempts = []
    run_status = "running"

    def checkpoint(result, source):
        report = store.save_source(result, source, checkpoint=True)
        key = (result.company, source.key)
        current = time.monotonic()
        if current - last_progress.get(key, 0) >= 15:
            last_progress[key] = current
            detail = report.get("metadata", {}).get("details")
            phase = f"; details checked {detail['checked']}/{detail['total']}" if detail else ""
            reused = f"; cached {detail['verified_cache_hits'] + detail['unavailable_cache_hits']}" if detail else ""
            print(f"{result.company}: {report['jobs_collected']} collected; {report['descriptions_verified']} descriptions verified{phase}{reused}", flush=True)
    try:
        async with HTTP(args.output, delay=args.delay, timeout=args.timeout, max_response_bytes=args.max_response_mb * 1024 * 1024) as http:
            async def collect(company, attempt, started):
                def finish_report(report):
                    report.update(started_at=attempt["started_at"], finished_at=now(),
                                  duration_seconds=round(elapsed(started), 6),
                                  timings={k: round(v, 6) for k, v in attempt["timings"].items()})
                    attempt["status"] = report["status"]
                    if store:
                        store.save_company(company, report)

                if not company.enabled:
                    report = {"company": company.name, "careers_url": company.careers_url, "status": "skipped",
                              "jobs_collected": 0, "descriptions_verified": 0, "sources": [],
                              "notes": company.notes, "errors": [], "warnings": ["skipped_by_user"], "finished_at": now()}
                    finish_report(report)
                    print(f"{company.name}: skipped at user request", flush=True)
                    return True
                expected_adapter = COMPANY_ADAPTERS.get(company.name)
                if store and expected_adapter is None:
                    review_block = COMPANY_REVIEW_BLOCKS.get(company.name)
                    finish_report({"company": company.name, "careers_url": company.careers_url, "status": "pending_review",
                                   "jobs_collected": 0, "descriptions_verified": 0, "sources": [],
                                   "errors": ["company_specific_website_review_incomplete" if review_block else "company_specific_website_flow_not_implemented"], "warnings": [],
                                   "notes": review_block or "Inspect this company's actual listing and exact detail journey before enabling collection."})
                    print(f"{company.name}: pending company-specific website implementation", flush=True)
                    return False
                discovery_started = time.perf_counter()
                path = args.output / "discovery" / f"{company.slug}.json"
                saved = (getattr(args, "discovery_dir", None) or path.parent) / path.name
                evidence = None
                if (args.resume or getattr(args, "discovery_dir", None)) and saved.exists():
                    raw = json.loads(saved.read_text())
                    if raw["company"] != company.name:
                        raise ValueError(f"Discovery evidence does not match registry: {saved}")
                    if raw["careers_url"] == company.careers_url:
                        sources = [company.resolve_source(Source(**s)) for s in raw["sources"]]
                        # Older audits may select account/application links or generic HTML.
                        # Revisit the company entry when its explicit website flow differs.
                        if not (expected_adapter and any(s.adapter != expected_adapter for s in sources)):
                            evidence = Discovery(**{**raw, "sources": list({s.key: s for s in sources}.values())})
                if evidence is None:
                    native_entry = detect(company.careers_url)
                    if (store and expected_adapter == 'arcesium' and native_entry
                            and native_entry.adapter == expected_adapter):
                        # This inspected company flow owns the exact public-header
                        # and same-page mapper requests. A generic preflight uses
                        # the wrong headers and can spend minutes retrying429s.
                        native_entry.evidence_url = company.careers_url
                        evidence = Discovery(company.name,company.careers_url,sources=[native_entry],
                                             edges=[{'from':company.careers_url,'to':native_entry.url,
                                                     'kind':'entry_delegated_to_inspected_company_flow'}])
                    else:
                        evidence = await discover(http, company, max_pages=args.discovery_pages, browser=args.browser)
                if (store and expected_adapter and expected_adapter in ADAPTERS
                        and not any(s.adapter == expected_adapter for s in evidence.sources)):
                    # Live discovery found only generic ATS boards (e.g. a Workday apply link) that the company's own
                    # reviewed flow does not enumerate. The company adapter is built around the registered official
                    # careers URL, which is exactly what reviewed evidence records, so run it from there.
                    evidence.sources = [*evidence.sources, Source(expected_adapter, company.careers_url,
                                                                  {"registered_company_flow": True},
                                                                  evidence_url=company.careers_url)]
                atomic_json(path, evidence.to_dict())
                attempt["timings"]["discovery_seconds"] = elapsed(discovery_started)
                if not store:
                    attempt["status"] = "failed" if evidence.errors else "discovered"
                    print(json.dumps({"company": company.name, "sources": [s.adapter for s in evidence.sources],
                                      "errors": evidence.errors}), flush=True)
                    return not evidence.errors
                source_reports = []
                sources_started = time.perf_counter()
                for source in evidence.sources:
                    if source.adapter != expected_adapter:
                        # An account/login/application link is not permission to enumerate an ATS board.
                        continue
                    # Loading recorded evidence cannot turn telemetry/vendor URLs into ATS boards.
                    if (source.adapter != "html" and not detect(source.url) and not source.options.get("detected_from_html")
                            and not source.options.get("registered_company_flow")):
                        continue
                    previous = store.saved_source(company.name, source.key)
                    adapter_type = ADAPTERS.get(source.adapter)
                    revision = getattr(adapter_type, "VERIFICATION_REVISION", None)
                    current_revision = not revision or (previous or {}).get("metadata", {}).get("verification_revision") == revision
                    if args.resume and previous and previous["status"] == "complete" and current_revision:
                        source_reports.append({**previous, "reused_from_completed_snapshot": True,
                                               "current_attempt_duration_seconds": 0.0})
                        attempt["sources_reused"] += 1
                        continue
                    attempt["sources_executed"] += 1
                    options = Options(args.max_jobs, args.max_pages, args.detail_concurrency, args.browser,
                                      checkpoint=lambda result: checkpoint(result, source),
                                      cached_jobs=store.cached_jobs(company.name, source.key) if args.resume else {},
                                      reuse_unavailable=not args.retry_unavailable,
                                      previous_report=previous if args.resume and previous else {})
                    if adapter_type is None:
                        result = Result(company.name, source.url, source.adapter,
                                        errors=["adapter_not_implemented"])
                    else:
                        adapter = adapter_type(http, company, source, options)
                        try:
                            result = await adapter.run()
                        except Exception as exc:
                            result = adapter.result
                            result.listing_complete = False
                            result.errors.append(f"{type(exc).__name__}: {exc}")
                    if company.scope != "company":
                        for job in result.jobs:
                            job.company_scope_verified = False
                            job.warnings.append("requested_division_scope_not_verified")
                    source_report = store.save_source(result, source)
                    source_reports.append(source_report)
                    for key, seconds in source_report["timings"].items():
                        attempt["timings"][key] = attempt["timings"].get(key, 0.0) + seconds
                attempt["timings"]["source_phase_seconds"] = elapsed(sources_started)
                complete = bool(source_reports) and all(r["status"] == "complete" for r in source_reports)
                errors = evidence.errors
                warnings = list(evidence.warnings)
                if company.scope != "company":
                    warnings.append("division_scope_not_verified")
                    complete = False
                if not source_reports:
                    errors = errors + ["no_verified_source_discovered"]
                count = sum(r["jobs_collected"] for r in source_reports)
                status = "complete" if complete and not errors and not warnings else "partial" if count else "failed"
                if args.max_jobs and any(r["status"] == "sampled" for r in source_reports):
                    status = "sampled"
                report = {"company": company.name, "careers_url": company.careers_url, "status": status,
                          "jobs_collected": count, "descriptions_verified": sum(r["descriptions_verified"] for r in source_reports),
                          "descriptions_unavailable": sum(r.get("descriptions_unavailable", 0) for r in source_reports),
                          "scope": company.scope, "notes": company.notes, "sources": source_reports,
                          "discovery_evidence": str(path), "errors": errors, "warnings": warnings, "finished_at": now()}
                finish_report(report)
                print(f"{company.name}: {status}; {count} jobs; {report['descriptions_verified']} descriptions verified; {format_duration(report['duration_seconds'])}", flush=True)
                return status == "complete"

            async def one(company):
                queued = time.perf_counter()
                attempt = {"company": company.name, "status": "queued", "started_at": None,
                           "finished_at": None, "duration_seconds": None, "queue_wait_seconds": None,
                           "sources_executed": 0, "sources_reused": 0, "timings": {}}
                attempts.append(attempt)
                started = None
                try:
                    async with semaphore:
                        started = time.perf_counter()
                        attempt.update(status="running", started_at=now(), queue_wait_seconds=round(elapsed(queued), 6))
                        return await collect(company, attempt, started)
                except BaseException as exc:
                    attempt["status"] = "interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "failed"
                    attempt["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    attempt["finished_at"] = now()
                    if started is not None:
                        attempt["duration_seconds"] = round(elapsed(started), 6)
                    else:
                        attempt["queue_wait_seconds"] = round(elapsed(queued), 6)
                    attempt["timings"] = {k: round(v, 6) for k, v in attempt["timings"].items()}

            tasks = [asyncio.create_task(one(c)) for c in companies]
            try:
                results = await asyncio.gather(*tasks)
                run_status = "completed"
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
    except BaseException as exc:
        run_status = "interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "failed"
        raise
    finally:
        export_timings = None
        try:
            if store:
                try:
                    store.export(include_jobs=not args.no_job_export)
                    export_timings = store.last_export_timings
                finally:
                    store.close()
        except BaseException:
            run_status = "export_failed"
            raise
        finally:
            timing_report = {"command": args.command, "started_at": run_started_at, "finished_at": now(),
                             "status": run_status, "duration_seconds": round(elapsed(run_started), 6),
                             "companies": attempts, "export": export_timings}
            atomic_json(args.output / "run-timings.json", timing_report)
            with (args.output / "timing-history.jsonl").open("a") as handle:
                handle.write(json.dumps(timing_report) + "\n")
        if store and args.no_job_export:
            print(f"Job-file export deferred; current data is in {args.output / 'jobs.sqlite3'}. Run `careers report` to generate JSONL/CSV.", flush=True)
    return 0 if all(results) else 2


def main():
    p = parser()
    args = p.parse_args()
    if args.command == "list":
        for company in load_registry(args.registry):
            print(f"{company.slug}\t{company.name}\t{company.careers_url}\t{'enabled' if company.enabled else 'skipped'}")
        return
    if args.command == "report":
        with output_lock(args.output):
            store = Store(args.output)
            try:
                reports = store.export()
                print(f"Exported {len(reports)} company reports to {args.output}")
            finally:
                store.close()
        return
    for name in ("concurrency", "discovery_pages", "max_jobs", "max_pages", "detail_concurrency", "max_response_mb"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            p.error(f"--{name.replace('_', '-')} must be positive")
    try:
        with output_lock(args.output):
            code = asyncio.run(execute(args))
    except (ValueError, KeyboardInterrupt) as exc:
        print(str(exc) or "Interrupted; saved completed sources remain resumable.", file=sys.stderr)
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    raise SystemExit(code)
