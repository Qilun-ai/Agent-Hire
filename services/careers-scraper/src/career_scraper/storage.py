import csv
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time

from .models import Job, now
from .timing import format_duration


@contextmanager
def output_lock(output):
    """One writer per snapshot directory; OS releases the lock after crashes."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".run.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(f"Another run is writing {output}; use a different output directory or wait.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"Another run is writing {output}; use a different output directory or wait.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


class Store:
    def __init__(self, output: Path):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(output / "jobs.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._saved_payloads = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                record_id TEXT PRIMARY KEY, company TEXT NOT NULL, source_key TEXT NOT NULL,
                data TEXT NOT NULL, fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                company TEXT NOT NULL, source_key TEXT NOT NULL, report TEXT NOT NULL,
                PRIMARY KEY(company, source_key)
            );
            CREATE TABLE IF NOT EXISTS companies (
                company TEXT PRIMARY KEY, report TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_company_order ON jobs(company, record_id);
            CREATE INDEX IF NOT EXISTS jobs_source_snapshot ON jobs(company, source_key);
        """)

    def saved_source(self, company, key):
        row = self.db.execute("SELECT report FROM sources WHERE company=? AND source_key=?", (company, key)).fetchone()
        return json.loads(row[0]) if row else None

    def cached_jobs(self, company, source_key):
        result = {}
        # Equal source keys traverse this index in rowid/insertion order. The
        # company/record-hash index instead caused random reads through gigabytes
        # of description blobs while resuming a single company on a slow volume.
        for (raw,) in self.db.execute("SELECT data FROM jobs INDEXED BY jobs_source_snapshot WHERE company=? AND source_key=?", (company, source_key)):
            data = json.loads(raw)
            data.pop("record_id", None)
            job = Job(**data)
            self._saved_payloads[job.key] = sha256(raw.encode()).digest()
            result[job.url] = job
            for alias in (job.raw.get("native_detail_url"), job.raw.get("absolute_url")):
                if isinstance(alias, str) and alias.startswith(("https://", "http://")):
                    result.setdefault(alias, job)
        return result

    def save_source(self, result, source, *, checkpoint=False):
        candidates = result.dirty_jobs if checkpoint and result.dirty_jobs else result.jobs
        if not checkpoint:
            result.metadata.pop('retained_previous_snapshot',None)
        if checkpoint:
            for job in candidates:
                result.validate_job(job)
        report = result.report(validate_jobs=not checkpoint)
        if not checkpoint:
            result.finish()
            report.update({"finished_at": result.finished_at, "duration_seconds": round(result.duration_seconds, 6),
                           "timings": {key: round(value, 6) for key, value in result.timings.items()}})
        if checkpoint:
            # Description hydration may finish before a handler's final listing/identity recheck.
            # Only the final save after run() returns may certify the source as complete.
            report["status"] = "partial"
            report["checkpoint"] = True
            report["checkpointed_at"] = now()
            report["finished_at"] = None
        saved_digests = {}
        deleted_keys = []
        def records():
            # Serialize into SQLite one row at a time. Materializing every job's
            # JSON again can double the memory of a large native company feed.
            for job in candidates if checkpoint else result.jobs:
                raw = json.dumps(job.to_dict(), ensure_ascii=False)
                digest = sha256(raw.encode()).digest()
                if self._saved_payloads.get(job.key) != digest:
                    saved_digests[job.key] = digest
                    yield (job.key, job.company, source.key, raw, job.fetched_at)
        with self.db:
            # Remove stale membership atomically, while retaining byte-identical
            # cached records. A small missing-market retry must not rewrite a
            # gigabyte of already saved full descriptions.
            preserve_previous = not checkpoint and not result.jobs and not report['listing_complete']
            if preserve_previous:
                retained = self.db.execute('SELECT count(*) FROM jobs WHERE company=? AND source_key=?',
                                           (result.company,source.key)).fetchone()[0]
                if retained:
                    report['metadata']['retained_previous_snapshot'] = {
                        'jobs':retained, 'current_listing_membership_verified':False,
                        'reason':'This attempt failed before obtaining a usable listing; previous records are retained for retry, not certified as current vacancies.'}
            if not checkpoint and not preserve_previous:
                self.db.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_keys (record_id TEXT PRIMARY KEY)")
                self.db.execute("DELETE FROM snapshot_keys")
                self.db.executemany("INSERT OR IGNORE INTO snapshot_keys VALUES (?)", ((job.key,) for job in result.jobs))
                deleted_keys = [row[0] for row in self.db.execute("SELECT record_id FROM jobs WHERE company=? AND source_key=? AND record_id NOT IN (SELECT record_id FROM snapshot_keys)",
                                                                  (result.company, source.key))]
                self.db.execute("DELETE FROM jobs WHERE company=? AND source_key=? AND record_id NOT IN (SELECT record_id FROM snapshot_keys)",
                                (result.company, source.key))
            self.db.executemany("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)", records())
            self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                (result.company, source.key, json.dumps(report)))
        for key in deleted_keys:
            self._saved_payloads.pop(key, None)
        self._saved_payloads.update(saved_digests)
        result.dirty_jobs.clear()
        return report

    def save_company(self, company, report):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO companies VALUES (?,?)", (company.name, json.dumps(report)))
        atomic_json(self.output / "companies" / f"{company.slug}.json", report)

    def _export_jobs(self):
        fields = ["company", "title", "job_id", "requisition_id", "url", "apply_url", "date_posted",
                  "date_posted_raw", "date_updated", "locations", "department", "employment_type",
                  "workplace_type", "salary", "description_text", "description_html",
                  "description_verified", "description_unavailable", "description_source_url", "fetched_at", "warnings"]
        with (self.output / "jobs.jsonl").open("w") as out, (self.output / "jobs.csv").open("w", newline="") as csv_out:
            writer = csv.DictWriter(csv_out, fieldnames=fields)
            writer.writeheader()
            for (raw,) in self.db.execute("SELECT data FROM jobs ORDER BY company, record_id"):
                out.write(raw + "\n")
                data = json.loads(raw)
                row = {}
                for key in fields:
                    value = data.get(key)
                    value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                    # Keep spreadsheet exports inert; JSONL retains the exact original values.
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                        value = "'" + value
                    row[key] = value
                writer.writerow(row)
    def export(self, *, include_jobs=True):
        started = time.perf_counter()
        job_export_seconds = None
        if include_jobs:
            self._export_jobs()
            job_export_seconds = max(0.0, time.perf_counter() - started)
        coverage_started = time.perf_counter()
        reports = [json.loads(row[0]) for row in self.db.execute("SELECT report FROM companies ORDER BY company")]
        retained = [{'company':r['company'],'source_url':source['source_url'],
                     **source['metadata']['retained_previous_snapshot']}
                    for r in reports for source in r.get('sources',[])
                    if source.get('metadata',{}).get('retained_previous_snapshot')]
        atomic_json(self.output / "coverage.json", {"generated_at": now(), "job_exports_deferred": not include_jobs,
                                                   "retained_previous_snapshots":retained,"companies": reports})
        lines = ["# Coverage report", "", "Complete means the observed source enumeration and description checks passed; company scope is reported separately.", "",
                 "| Company | Status | Jobs | Verified descriptions | Runtime | Sources |", "|---|---|---:|---:|---|---|"]
        for r in reports:
            lines.append(f"| {r['company']} | {r['status']} | {r['jobs_collected']} | {r['descriptions_verified']} | {format_duration(r.get('duration_seconds'))} | {', '.join(s['adapter'] for s in r.get('sources', []))} |")
        if retained:
            lines += ['', 'Some failed sources retained prior records for retry. Their current listing membership is unverified; see retained_previous_snapshots in coverage.json and each record’s original fetched_at. These rows are not a successful current collection.']
        if not include_jobs:
            lines += ["", "Job-file export was deferred. SQLite contains current collected data; existing JSONL/CSV files may describe an earlier snapshot. Run `careers report` to regenerate them."]
        (self.output / "coverage.md").write_text("\n".join(lines) + "\n")
        finished = time.perf_counter()
        self.last_export_timings = {"generated_at": now(), "job_files_exported": include_jobs,
            "duration_seconds": round(max(0.0, finished - started), 6),
            "job_export_seconds": round(job_export_seconds, 6) if job_export_seconds is not None else None,
            "coverage_export_seconds": round(max(0.0, finished - coverage_started), 6)}
        atomic_json(self.output / "export-timings.json", self.last_export_timings)
        return reports

    def close(self):
        self.db.close()
