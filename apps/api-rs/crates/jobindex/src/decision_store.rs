//! Queue queries and atomic verdict writes for the job index.

use rusqlite::{Connection, params};
use serde_json::{Value, json};

use crate::autoclassify::ROLE_KEY as SOFTWARE_ROLE_KEY;
use crate::decision::STATE_CHARS;
use crate::departments::{DEPARTMENT_ROLE_KEY, LEVEL_ROLE_KEY};
use crate::store::{JobIndex, now_iso, posted_expr};

/// A posting snapshot selected for classification.
#[derive(Clone, Debug)]
pub struct QueuedJob {
    /// The index's job ID.
    pub id: String,
    /// The unprocessed job title.
    pub title: String,
    /// The employer.
    pub company: String,
    /// The employer's department or team.
    pub department: String,
    /// The job description.
    pub description: String,
    /// The posting revision when it was selected.
    pub content_hash: Option<String>,
}

/// A role or encoder verdict to record for a posting snapshot.
#[derive(Clone, Debug)]
pub struct QueuedVerdict<'a> {
    /// The posting revision used to make the prediction.
    pub job: &'a QueuedJob,
    /// Whether the job matches the requested role.
    pub role_match: bool,
    /// The model's probability.
    pub probability: Option<f64>,
    /// The predicted level or department code.
    pub label: Option<&'a str>,
}

/// Returns unjudged active postings, newest first. The software backlog uses a description prefix.
pub fn pending_jobs(
    index: &JobIndex,
    role_key: &str,
    limit: i64,
) -> rusqlite::Result<Vec<QueuedJob>> {
    let connection = index.connect()?;
    let (description, filter) = if role_key == SOFTWARE_ROLE_KEY {
        (
            format!("substr(j.description,1,{STATE_CHARS})"),
            "AND j.rule_software=1",
        )
    } else {
        ("j.description".to_owned(), "")
    };
    let sql = format!(
        "SELECT j.id,j.title,j.company,j.department,{description},j.content_hash
         FROM jobs j LEFT JOIN decisions d ON d.job_id=j.id AND d.role_key=?1
         WHERE j.active=1 AND d.job_id IS NULL {filter}
         ORDER BY {} DESC LIMIT ?2",
        posted_expr("j")
    );
    let mut statement = connection.prepare(&sql)?;
    statement
        .query_map(params![role_key, limit], |row| {
            Ok(QueuedJob {
                id: row.get(0)?,
                title: row.get(1)?,
                company: row.get(2)?,
                department: row.get::<_, Option<_>>(3)?.unwrap_or_default(),
                description: row.get::<_, Option<_>>(4)?.unwrap_or_default(),
                content_hash: row.get(5)?,
            })
        })?
        .collect()
}

/// Stores a verdict batch, skipping postings that were removed, closed, or revised during inference.
pub fn save_verdicts(
    index: &JobIndex,
    role_key: &str,
    source: &str,
    verdicts: &[QueuedVerdict<'_>],
) -> rusqlite::Result<usize> {
    let mut connection = index.connect()?;
    let transaction = connection.transaction()?;
    let stamp = now_iso();
    let mut saved = 0;
    {
        let mut statement = transaction.prepare_cached(
            "INSERT INTO decisions(job_id, role_key, role_match, role_p, level, source, decided_at)
             SELECT id, ?2, ?3, ?4, ?5, ?6, ?7 FROM jobs
             WHERE id=?1 AND active=1 AND content_hash IS ?8
             ON CONFLICT(job_id, role_key) DO UPDATE SET role_match=excluded.role_match,
             role_p=excluded.role_p, level=excluded.level, source=excluded.source, decided_at=excluded.decided_at"
        )?;
        for verdict in verdicts {
            saved += statement.execute(params![
                verdict.job.id,
                role_key,
                i64::from(verdict.role_match),
                verdict.probability,
                verdict.label,
                source,
                stamp,
                verdict.job.content_hash
            ])?;
        }
    }
    transaction.commit()?;
    Ok(saved)
}

/// Returns the number of active postings and the number with a verdict for this encoder.
pub fn encoder_counts(connection: &Connection, role_key: &str) -> rusqlite::Result<(i64, i64)> {
    connection.query_row(
        "SELECT COUNT(*),COUNT(d.job_id) FROM jobs j
         LEFT JOIN decisions d ON d.job_id=j.id AND d.role_key=?1 WHERE j.active=1",
        [role_key],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
}

/// Builds encoder progress for the top bar using one database snapshot.
pub fn encoder_progress(
    connection: &Connection,
    level: bool,
    department: bool,
    device: &str,
) -> rusqlite::Result<Value> {
    connection.query_row(
        "SELECT COUNT(*), COUNT(d.job_id), COUNT(l.job_id),
                COUNT(CASE WHEN d.job_id IS NOT NULL THEN l.job_id END)
         FROM jobs j LEFT JOIN decisions d ON d.job_id=j.id AND d.role_key=?1
         LEFT JOIN decisions l ON l.job_id=j.id AND l.role_key=?2 WHERE j.active=1",
        [DEPARTMENT_ROLE_KEY, LEVEL_ROLE_KEY],
        |row| {
            let active: i64 = row.get(0)?;
            let classified: i64 = row.get(3)?;
            Ok(json!({
                "active": active, "classified": classified, "remaining": active - classified,
                "department_done": row.get::<_, i64>(1)?, "level_done": row.get::<_, i64>(2)?,
                "models": {"department": department, "level": level}, "device": device,
            }))
        },
    )
}
