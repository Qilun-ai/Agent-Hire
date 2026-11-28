//! Scrape runs: launch the careers scraper (a separate Python CLI) and import companies as
//! their completion reports appear.
//!
//! Runs live in the `runs` table with the scraper's process id, so any process can see and stop
//! them. Only one run executes at a time.

use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::fs::File;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, PoisonError};

use agent_hire_store::{SettingsStore, python_json_dumps};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::{Map, Value, json};

use crate::datetime::DateTime;
use crate::store::{self, JobIndex, row_object};

const MAX_COMPANIES_PER_RUN: usize = 400;
static RUN_MUTATION: Mutex<()> = Mutex::new(());

/// Interval between scrape import and refresh ticks.
pub const SCRAPE_TICK_SECONDS: u64 = 30;

/// Whether a scrape tick also reconciles exited runs and checks refresh and rule versions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TickMode {
    /// Import completed companies and run the full periodic maintenance.
    Periodic,
    /// Only import completed companies from the active scrape.
    ImportOnly,
}

/// One speed category: key, label, range text and upper bound in seconds.
pub const SPEED_CATEGORIES: [(&str, &str, &str, Option<f64>); 5] = [
    ("fast", "Fast", "under 1 min", Some(60.0)),
    ("quick", "Quick", "1–5 min", Some(300.0)),
    ("medium", "Medium", "5–15 min", Some(900.0)),
    ("slow", "Slow", "15–60 min", Some(3600.0)),
    ("very_slow", "Very slow", "over 1 hour", None),
];

/// The category keys a scrape may ask for.
pub const SPEED_KEYS: [&str; 6] = ["fast", "quick", "medium", "slow", "very_slow", "unknown"];

/// Where the scraper lives: `AGENT_HIRE_SCRAPER_DIR`, else `services/careers-scraper`.
pub fn scraper_dir(repo_root: Option<&Path>) -> PathBuf {
    match env::var("AGENT_HIRE_SCRAPER_DIR")
        .ok()
        .map(|dir| dir.trim().to_owned())
        .filter(|dir| !dir.is_empty())
    {
        Some(dir) => PathBuf::from(dir),
        None => repo_root.map_or_else(
            || PathBuf::from("services/careers-scraper"),
            |root| root.join("services").join("careers-scraper"),
        ),
    }
}

/// Every company the scraper knows (`companies.json`).
pub fn catalog(scraper: &Path) -> Vec<Map<String, Value>> {
    let Ok(text) = fs::read_to_string(scraper.join("companies.json")) else {
        return Vec::new();
    };
    serde_json::from_str::<Value>(&text)
        .ok()
        .and_then(|value| value["companies"].as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|row| row.as_object().cloned())
        .collect()
}

fn text<'a>(row: &'a Map<String, Value>, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or("")
}

/// The slugs of verified companies (scraped by default).
pub fn verified_slugs(scraper: &Path) -> Vec<String> {
    catalog(scraper)
        .iter()
        .filter(|row| text(row, "status") == "verified")
        .map(|row| text(row, "slug").to_owned())
        .collect()
}

/// The category for a scrape duration.
pub fn speed_category(seconds: Option<f64>) -> &'static str {
    let Some(seconds) = seconds else {
        return "unknown";
    };
    SPEED_CATEGORIES
        .iter()
        .find(|(_, _, _, max)| max.is_none_or(|max| seconds < max))
        .map_or("very_slow", |(key, _, _, _)| key)
}

/// `{slug: {seconds, basis, category}}`: our own last measurement wins over the recorded timing.
pub fn company_speeds(index: &JobIndex, scraper: &Path) -> HashMap<String, Value> {
    let mut measured: HashMap<String, f64> = HashMap::new();
    if let Ok(connection) = index.connect()
        && let Ok(mut statement) = connection.prepare("SELECT slug, last_duration_seconds FROM companies WHERE last_duration_seconds IS NOT NULL")
        && let Ok(rows) = statement.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?)))
    {
        measured.extend(rows.filter_map(Result::ok));
    }
    let mut out = HashMap::new();
    for row in catalog(scraper) {
        let slug = text(&row, "slug").to_owned();
        let (seconds, basis) = match measured.get(&slug) {
            Some(seconds) => (Some(*seconds), "measured"),
            None => (
                row.get("recorded_seconds").and_then(Value::as_f64),
                "recorded",
            ),
        };
        out.insert(
            slug,
            json!({"seconds": seconds, "basis": seconds.map(|_| basis), "category": speed_category(seconds)}),
        );
    }
    out
}

fn resolve_companies(scraper: &Path, names: &[String]) -> (Vec<String>, Vec<String>) {
    let rows = catalog(scraper);
    if names.is_empty() {
        return (verified_slugs(scraper), Vec::new());
    }
    let mut found: Vec<String> = Vec::new();
    let mut unknown = Vec::new();
    for name in names {
        let key = name.trim();
        let folded = key.to_lowercase();
        let by_slug = rows.iter().find(|row| text(row, "slug") == folded);
        let by_name = || {
            rows.iter()
                .find(|row| text(row, "company").to_lowercase() == folded)
        };
        let partial = || {
            let candidates: Vec<&Map<String, Value>> = rows
                .iter()
                .filter(|row| text(row, "company").to_lowercase().contains(&folded))
                .collect();
            (candidates.len() == 1).then(|| candidates[0])
        };
        match by_slug.or_else(by_name).or_else(partial) {
            Some(row) => {
                let slug = text(row, "slug").to_owned();
                if !found.contains(&slug) {
                    found.push(slug);
                }
            }
            None => unknown.push(name.clone()),
        }
    }
    (found, unknown)
}

/// The running run, if any.
pub fn active_run(connection: &Connection) -> rusqlite::Result<Option<Map<String, Value>>> {
    connection
        .query_row(
            "SELECT * FROM runs WHERE status='running' ORDER BY started_at DESC LIMIT 1",
            [],
            row_object,
        )
        .optional()
}

/// Whether a process is alive (a zombie counts as gone).
fn alive(pid: Option<i64>) -> bool {
    let Some(pid) = pid.filter(|pid| *pid > 0) else {
        return false;
    };
    if cfg!(target_os = "linux") {
        return match fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) => !stat
                .rsplit(')')
                .next()
                .is_some_and(|rest| rest.trim_start().starts_with('Z')),
            Err(_) => false,
        };
    }
    if cfg!(windows) {
        // `tasklist` prints the matching process as CSV, or an informational line without quotes.
        return Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
            .stderr(Stdio::null())
            .output()
            .is_ok_and(|out| String::from_utf8_lossy(&out.stdout).contains(&format!("\"{pid}\"")));
    }
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|s| s.success())
}

/// Stops a scrape and the processes it started (its process group on Unix, its tree on Windows).
fn stop_process_tree(pid: i64) {
    if cfg!(windows) {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    } else {
        let _ = Command::new("kill")
            .args(["-TERM", "--", &format!("-{pid}")])
            .stderr(Stdio::null())
            .status();
    }
}

fn json_list(row: &Map<String, Value>, key: &str) -> Vec<String> {
    serde_json::from_str::<Vec<String>>(text(row, key)).unwrap_or_default()
}

/// The public view of a run.
pub fn public(row: Option<&Map<String, Value>>) -> Value {
    let Some(row) = row else { return Value::Null };
    let planned = json_list(row, "companies");
    let imported: HashSet<String> = json_list(row, "imported").into_iter().collect();
    let remaining: Vec<&String> = planned
        .iter()
        .filter(|slug| !imported.contains(*slug))
        .take(20)
        .collect();
    json!({
        "id": row.get("id"), "kind": row.get("kind"), "status": row.get("status"), "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"), "companies_planned": planned.len(), "companies_done": imported.len(),
        "new_jobs": row.get("new_jobs"), "seen_jobs": row.get("seen_jobs"), "remaining": remaining,
    })
}

/// `{active, recent, refresh_hours}`, or `{run}` for one run id.
pub fn run_status(
    index: &JobIndex,
    run_id: Option<&str>,
    refresh_hours: f64,
) -> rusqlite::Result<Value> {
    let connection = index.connect()?;
    if let Some(run_id) = run_id {
        let row = connection
            .query_row("SELECT * FROM runs WHERE id=?1", [run_id], row_object)
            .optional()?;
        return Ok(json!({ "run": public(row.as_ref()) }));
    }
    let current = active_run(&connection)?;
    let mut statement =
        connection.prepare("SELECT * FROM runs ORDER BY started_at DESC LIMIT 5")?;
    let recent: Vec<Value> = statement
        .query_map([], row_object)?
        .filter_map(Result::ok)
        .map(|row| public(Some(&row)))
        .collect();
    Ok(
        json!({ "active": public(current.as_ref()), "recent": recent, "refresh_hours": refresh_hours }),
    )
}

/// The refresh interval from the `scrape_refresh_hours` setting (default 12; 0 turns it off).
pub fn refresh_hours(setting: Option<&str>) -> f64 {
    setting
        .unwrap_or("12")
        .trim()
        .parse::<f64>()
        .map_or(12.0, |hours| hours.max(0.0))
}

fn runs_root(index: &JobIndex) -> PathBuf {
    let path = match env::var("AGENT_HIRE_SCRAPE_RUNS_DIR")
        .ok()
        .map(|dir| dir.trim().to_owned())
        .filter(|dir| !dir.is_empty())
    {
        Some(dir) => PathBuf::from(dir),
        None => index.dir().join("runs"),
    };
    let _ = fs::create_dir_all(&path);
    path
}

fn uv() -> PathBuf {
    let found = env::var_os("PATH").and_then(|paths| {
        env::split_paths(&paths)
            .map(|dir| dir.join(if cfg!(windows) { "uv.exe" } else { "uv" }))
            .find(|candidate| candidate.is_file())
    });
    found.unwrap_or_else(|| {
        env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default()
            .join(".local")
            .join("bin")
            .join("uv")
    })
}

/// Starts a scrape. No companies and no categories means every verified company.
pub fn start_run(
    index: &JobIndex,
    scraper: &Path,
    companies: &[String],
    categories: &[String],
    concurrency: Option<i64>,
    kind: &str,
) -> rusqlite::Result<Value> {
    let speeds = company_speeds(index, scraper);
    let (mut slugs, unknown) = if categories.is_empty() {
        resolve_companies(scraper, companies)
    } else {
        let wanted: HashSet<&str> = categories.iter().map(String::as_str).collect();
        let mut by_category: Vec<String> = verified_slugs(scraper)
            .into_iter()
            .filter(|slug| {
                wanted.contains(
                    speeds
                        .get(slug)
                        .and_then(|s| s["category"].as_str())
                        .unwrap_or("unknown"),
                )
            })
            .collect();
        let (named, unknown) = if companies.is_empty() {
            (Vec::new(), Vec::new())
        } else {
            resolve_companies(scraper, companies)
        };
        for slug in named {
            if !by_category.contains(&slug) {
                by_category.push(slug);
            }
        }
        (by_category, unknown)
    };
    if slugs.is_empty() {
        return Ok(json!({ "started": false, "reason": "no_known_companies", "unknown": unknown }));
    }
    // Fastest first, so quick boards become searchable within minutes.
    let seconds_of = |slug: &String| {
        speeds
            .get(slug)
            .and_then(|s| s["seconds"].as_f64())
            .unwrap_or(1e9)
    };
    slugs.sort_by(|a, b| {
        seconds_of(a)
            .partial_cmp(&seconds_of(b))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    slugs.truncate(MAX_COMPANIES_PER_RUN);
    let _mutation = RUN_MUTATION.lock().unwrap_or_else(PoisonError::into_inner);
    let connection = index.connect()?;
    if let Some(running) = active_run(&connection)? {
        if alive(running.get("pid").and_then(Value::as_i64)) {
            return Ok(
                json!({ "started": false, "reason": "already_running", "run": public(Some(&running)), "unknown": unknown }),
            );
        }
        import_finished_companies(&connection, &running)?;
        connection.execute(
            "UPDATE runs SET status='interrupted', finished_at=?1 WHERE id=?2",
            params![store::now_iso(), text(&running, "id")],
        )?;
    }
    let stamp = DateTime::now_utc().isoformat_seconds();
    let compact: String = stamp[..19].replace(['-', ':'], "");
    let mut random = [0_u8; 3];
    let _ = getrandom::fill(&mut random);
    let run_id = format!(
        "{compact}Z-{}",
        random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    let out = runs_root(index).join(&run_id);
    let _ = fs::create_dir_all(&out);
    let workers = concurrency.unwrap_or_else(|| {
        env::var("AGENT_HIRE_SCRAPE_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4)
    });
    let mut command = Command::new(uv());
    command
        .arg("run")
        .arg("--project")
        .arg(scraper)
        .args(["--quiet", "careers", "scrape", "--output"])
        .arg(&out);
    command.args([
        "--no-job-export",
        "--concurrency",
        &workers.max(1).to_string(),
    ]);
    for slug in &slugs {
        command.args(["--company", slug]);
    }
    let log = File::options()
        .create(true)
        .append(true)
        .open(out.join("scrape.log"));
    let (stdout, stderr) = match log.and_then(|log| Ok((log.try_clone()?, log))) {
        Ok((first, second)) => (Stdio::from(first), Stdio::from(second)),
        Err(_) => (Stdio::null(), Stdio::null()),
    };
    command
        .current_dir(scraper)
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;

        command.process_group(0);
    }
    let child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Ok(
                json!({ "started": false, "reason": "spawn_failed", "error": error.to_string(), "unknown": unknown }),
            );
        }
    };
    let pid = i64::from(child.id());
    // Reap the child when it exits so it does not linger as a zombie.
    std::thread::spawn(move || {
        let mut child = child;
        let _ = child.wait();
    });
    connection.execute(
        "INSERT INTO runs(id, kind, status, started_at, companies, pid, output_dir) VALUES(?1,?2,?3,?4,?5,?6,?7)",
        params![run_id, kind, "running", store::now_iso(), python_json_dumps(&json!(slugs), true), pid, out.to_string_lossy()],
    )?;
    let row = connection.query_row("SELECT * FROM runs WHERE id=?1", [&run_id], row_object)?;
    Ok(json!({ "started": true, "run": public(Some(&row)), "unknown": unknown }))
}

/// Stops the running (or named) run, imports what it finished and keeps its log.
pub fn stop_run(index: &JobIndex, run_id: Option<&str>) -> rusqlite::Result<Value> {
    let _mutation = RUN_MUTATION.lock().unwrap_or_else(PoisonError::into_inner);
    let connection = index.connect()?;
    let row = match run_id {
        Some(id) => connection
            .query_row("SELECT * FROM runs WHERE id=?1", [id], row_object)
            .optional()?,
        None => active_run(&connection)?,
    };
    let Some(row) = row.filter(|row| text(row, "status") == "running") else {
        return Ok(json!({ "stopped": false, "reason": "not_running" }));
    };
    if let Some(pid) = row
        .get("pid")
        .and_then(Value::as_i64)
        .filter(|pid| alive(Some(*pid)))
    {
        stop_process_tree(pid);
    }
    import_finished_companies(&connection, &row)?;
    connection.execute(
        "UPDATE runs SET status='stopped', finished_at=?1 WHERE id=?2 AND status='running'",
        params![store::now_iso(), text(&row, "id")],
    )?;
    let fresh = connection.query_row(
        "SELECT * FROM runs WHERE id=?1",
        [text(&row, "id")],
        row_object,
    )?;
    cleanup(text(&row, "output_dir"));
    Ok(json!({ "stopped": true, "run": public(Some(&fresh)) }))
}

fn report_complete(report: &Value) -> bool {
    if matches!(report["status"].as_str(), Some("complete" | "verified")) {
        return true;
    }
    let sources = report["sources"].as_array().cloned().unwrap_or_default();
    !sources.is_empty()
        && sources
            .iter()
            .all(|s| s["listing_complete"].as_bool().unwrap_or(false))
        && report["jobs_collected"].as_i64().unwrap_or(0) > 0
}

/// Imports every company whose completion report has appeared and is not yet imported.
pub fn import_finished_companies(
    connection: &Connection,
    run: &Map<String, Value>,
) -> rusqlite::Result<usize> {
    import_with_notify(connection, run, &mut || {})
}

fn import_with_notify(
    connection: &Connection,
    run: &Map<String, Value>,
    notify: &mut impl FnMut(),
) -> rusqlite::Result<usize> {
    let mut imported = json_list(run, "imported");
    let output_dir = text(run, "output_dir");
    if output_dir.is_empty() {
        return Ok(imported.len());
    }
    let out = PathBuf::from(output_dir);
    let reports = out.join("companies");
    let database = out.join("jobs.sqlite3");
    if !reports.is_dir() || !database.exists() {
        return Ok(imported.len());
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(&reports)
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| p.extension().is_some_and(|x| x == "json"))
                .collect()
        })
        .unwrap_or_default();
    paths.sort();
    let run_id = text(run, "id").to_owned();
    for path in paths {
        let slug = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        if imported.contains(&slug) {
            continue;
        }
        let Some(report) = fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        else {
            continue; // being written right now; next pass
        };
        let company = report["company"]
            .as_str()
            .filter(|c| !c.is_empty())
            .unwrap_or(&slug)
            .to_owned();
        let duration = report["duration_seconds"].as_f64().or_else(|| {
            let total: f64 = report["sources"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|s| s["duration_seconds"].as_f64())
                .sum();
            (total != 0.0).then_some(total)
        });
        let records = company_records(&database, &company)?;
        let transaction = Transaction::new_unchecked(connection, TransactionBehavior::Immediate)?;
        let current =
            transaction.query_row("SELECT * FROM runs WHERE id=?1", [&run_id], row_object)?;
        imported = json_list(&current, "imported");
        if imported.contains(&slug) {
            continue;
        }
        let counts = store::import_company(
            &transaction,
            &slug,
            &company,
            records.into_iter(),
            &run_id,
            report_complete(&report),
            duration,
        )?;
        imported.push(slug);
        imported.sort();
        transaction.execute(
            "UPDATE runs SET imported=?1, new_jobs=new_jobs+?2, seen_jobs=seen_jobs+?3 WHERE id=?4",
            params![
                python_json_dumps(&json!(imported), true),
                counts.new as i64,
                counts.seen as i64,
                run_id
            ],
        )?;
        transaction.commit()?;
        notify();
    }
    Ok(imported.len())
}

fn company_records(database: &Path, company: &str) -> rusqlite::Result<Vec<Map<String, Value>>> {
    let source = Connection::open_with_flags(database, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    source.busy_timeout(std::time::Duration::from_secs(30))?;
    let mut statement = source.prepare("SELECT data FROM jobs WHERE company=?1")?;
    let rows = statement.query_map([company], |row| row.get::<_, String>(0))?;
    let mut records = Vec::new();
    for row in rows {
        if let Ok(record) = serde_json::from_str::<Map<String, Value>>(&row?) {
            records.push(record);
        }
    }
    Ok(records)
}

/// Imports newly finished companies and runs periodic maintenance when requested.
///
/// The callback runs after each company commits, so workers can see its new jobs.
pub fn tick(
    index: &JobIndex,
    settings: &SettingsStore,
    scraper: &Path,
    mode: TickMode,
    mut notify: impl FnMut(),
) -> rusqlite::Result<()> {
    {
        let _mutation = RUN_MUTATION.lock().unwrap_or_else(PoisonError::into_inner);
        let connection = index.connect()?;
        if let Some(run) = active_run(&connection)? {
            import_with_notify(&connection, &run, &mut notify)?;
            if mode == TickMode::Periodic && !alive(run["pid"].as_i64()) {
                let id = text(&run, "id");
                let fresh =
                    connection.query_row("SELECT * FROM runs WHERE id=?1", [id], row_object)?;
                let done = import_with_notify(&connection, &fresh, &mut notify)?;
                let status = if done >= json_list(&fresh, "companies").len() {
                    "finished"
                } else {
                    "finished_partial"
                };
                finish_run(&connection, &fresh, status)?;
            }
        }
    }
    if mode == TickMode::Periodic {
        maybe_start_refresh(index, settings, scraper)?;
        store::recompute_rules(&mut index.connect()?, 8000)?;
    }
    Ok(())
}

/// Imports saved companies and marks an exited active scrape as interrupted at app startup.
pub fn interrupt_orphans(index: &JobIndex, mut notify: impl FnMut()) -> rusqlite::Result<()> {
    let _mutation = RUN_MUTATION.lock().unwrap_or_else(PoisonError::into_inner);
    let connection = index.connect()?;
    if let Some(run) = active_run(&connection)?
        && !alive(run["pid"].as_i64())
    {
        import_with_notify(&connection, &run, &mut notify)?;
        finish_run(&connection, &run, "interrupted")?;
    }
    Ok(())
}

fn finish_run(
    connection: &Connection,
    run: &Map<String, Value>,
    status: &str,
) -> rusqlite::Result<()> {
    let updated = connection.execute(
        "UPDATE runs SET status=?1, finished_at=?2 WHERE id=?3 AND status='running'",
        params![status, store::now_iso(), text(run, "id")],
    )?;
    if updated != 0 {
        cleanup(text(run, "output_dir"));
    }
    Ok(())
}

/// Starts a full refresh when a prior full scrape is old enough and the index is idle.
pub fn maybe_start_refresh(
    index: &JobIndex,
    settings: &SettingsStore,
    scraper: &Path,
) -> rusqlite::Result<Option<Value>> {
    let value = settings
        .get("scrape_refresh_hours", "12")
        .unwrap_or_else(|_| "12".to_owned());
    let hours = refresh_hours(Some(&value));
    let connection = index.connect()?;
    if !refresh_due(&connection, hours, DateTime::now_utc())? {
        return Ok(None);
    }
    start_run(index, scraper, &[], &[], None, "refresh").map(Some)
}

fn refresh_due(connection: &Connection, hours: f64, now: DateTime) -> rusqlite::Result<bool> {
    if !hours.is_finite() || hours <= 0.0 || active_run(connection)?.is_some() {
        return Ok(false);
    }
    let last: Option<String> = connection
        .query_row(
            "SELECT started_at FROM runs WHERE kind IN ('refresh','manual')
         AND json_array_length(companies)>=50 ORDER BY started_at DESC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .optional()?;
    let due = last
        .as_deref()
        .and_then(DateTime::parse_absolute)
        .is_some_and(|started| {
            now.micros.saturating_sub(started.micros) as f64 >= hours * 3_600_000_000.0
        });
    Ok(due)
}

/// Keeps the log and summary; drops raw responses and the scraper's working database.
fn cleanup(output_dir: &str) {
    if output_dir.is_empty() {
        return;
    }
    let out = Path::new(output_dir);
    for name in ["responses", "discovery", "browser"] {
        let _ = fs::remove_dir_all(out.join(name));
    }
    for name in ["jobs.sqlite3", "requests.jsonl"] {
        let _ = fs::remove_file(out.join(name));
    }
}

#[cfg(test)]
mod tests {
    use rusqlite::{Connection, params};
    use serde_json::json;

    use super::{DateTime, refresh_due, refresh_hours, speed_category};

    #[test]
    fn schedules_refresh_only_after_a_due_full_scrape() {
        let connection = Connection::open_in_memory().expect("open an in-memory index");
        connection
            .execute_batch(
                "CREATE TABLE runs(id TEXT,kind TEXT,status TEXT,started_at TEXT,companies TEXT);",
            )
            .expect("create fixture runs table");
        let now = DateTime::parse_absolute("2026-11-26T12:00:00Z").expect("parse fixture time");
        let started = "2026-11-26T00:00:00Z";
        let full = json!(vec!["company"; 50]).to_string();
        connection
            .execute(
                "INSERT INTO runs VALUES('seed','seed','finished',?1,?2)",
                params![started, full],
            )
            .expect("seed import");
        connection
            .execute(
                "INSERT INTO runs VALUES('small','manual','finished',?1,'[1]')",
                [started],
            )
            .expect("small manual run");
        assert!(!refresh_due(&connection, 12.0, now).expect("check missing full scrape"));
        connection
            .execute(
                "INSERT INTO runs VALUES('full','manual','finished',?1,?2)",
                params![started, full],
            )
            .expect("full manual run");
        assert!(refresh_due(&connection, 12.0, now).expect("check due refresh"));
        assert!(!refresh_due(&connection, 24.0, now).expect("check recent refresh"));
        assert!(!refresh_due(&connection, 0.0, now).expect("check disabled refresh"));
        connection
            .execute("UPDATE runs SET status='running' WHERE id='full'", [])
            .expect("start run");
        assert!(!refresh_due(&connection, 12.0, now).expect("check active run"));
    }

    #[test]
    fn categories() {
        assert_eq!(speed_category(None), "unknown");
        assert_eq!(speed_category(Some(30.0)), "fast");
        assert_eq!(speed_category(Some(299.0)), "quick");
        assert_eq!(speed_category(Some(7200.0)), "very_slow");
        assert_eq!(refresh_hours(Some("0")), 0.0);
        assert_eq!(refresh_hours(Some("x")), 12.0);
    }
}
