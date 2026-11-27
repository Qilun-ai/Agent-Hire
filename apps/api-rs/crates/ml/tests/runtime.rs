//! Server-facing route shapes and the job encoder worker lifecycle.

use std::fs;
use std::path::PathBuf;

use agent_hire_jobindex::{self as jobindex, JobIndex, ModelDirectories};
use agent_hire_ml::JobIndexRuntime;
use agent_hire_store::{Database, SettingsStore};
use serde_json::json;

struct Fixture {
    dir: PathBuf,
    index: JobIndex,
    settings: SettingsStore,
}

impl Fixture {
    fn new(test: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "agent-hire-ml-runtime-{}-{test}",
            std::process::id()
        ));
        fs::create_dir(&dir).expect("create fixture directory");
        let index = JobIndex::new(&dir);
        let settings = SettingsStore::new(
            Database::open(&dir.join("app.sqlite3")).expect("settings database"),
        );
        Self {
            dir,
            index,
            settings,
        }
    }

    fn runtime(&self, models: ModelDirectories) -> JobIndexRuntime {
        JobIndexRuntime::with_models(
            self.index.clone(),
            self.settings.clone(),
            self.dir.clone(),
            models,
        )
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.dir).expect("remove fixture directory");
    }
}

#[test]
fn builds_python_route_responses_for_a_one_job_index() {
    let fixture = Fixture::new("routes");
    fs::write(
        fixture.dir.join("companies.json"),
        r#"{"companies":[{"slug":"acme","status":"verified"}]}"#,
    )
    .expect("catalog fixture");
    let runtime = fixture.runtime(ModelDirectories {
        level: fixture.dir.join("absent-level"),
        department: fixture.dir.join("absent-department"),
    });
    assert_eq!(
        runtime.progress().expect("empty progress"),
        json!({
            "active":0,"classified":0,"remaining":0,"department_done":0,"level_done":0,
            "models":{"department":false,"level":false},"device":"not loaded",
        })
    );
    let db = fixture.index.connect().expect("open job index");
    jobindex::add_custom_job(
        &db,
        "Backend Engineer",
        "Acme",
        "Build API services in Rust.",
        "https://example.test/jobs/123",
        "Toronto, Canada",
        "hybrid",
    )
    .expect("add job");
    runtime.notify();
    let status = runtime.status().expect("read status");
    assert_eq!(
        status,
        json!({
            "classification":{"by_source":{},"classified":0,"remaining":1,"software_jobs":1},
            "companies_available":1,
            "decision_endpoints":[],
            "department_model":{"active_jobs":1,"judged":0,"model":false,"remaining":1},
            "index":{"companies_indexed":0,"jobs_active":1,"jobs_last_24h":1,"jobs_software":1,
                     "jobs_total":1,"jobs_with_published_date":0,"last_import_at":null},
            "jobs_dir":fixture.dir,
            "level_model":{"active_jobs":1,"device":"not loaded","judged":0,"model":false,"remaining":1},
            "scrape":{"active":null,"recent":[],"refresh_hours":12.0},
        })
    );
    runtime.autoclassifier().tick();
    assert_eq!(
        runtime.classify(8).expect("classify route"),
        json!({
            "skipped":"no_decision_model",
            "backlog":{"by_source":{},"classified":0,"remaining":1,"software_jobs":1},
        })
    );
    assert_eq!(
        runtime.probe(),
        json!({"configured":false,"fallback":"rules","results":[]})
    );
    runtime.start().expect("start workers");
    runtime.start().expect("idempotent start");
    runtime.stop();
    assert_eq!(runtime.device(), "not loaded");
}

#[test]
#[ignore = "requires the shipped quantized model directories"]
fn notify_runs_both_workers_and_persists_encoder_verdicts() {
    use std::env;
    use std::thread;
    use std::time::{Duration, Instant};

    let fixture = Fixture::new("workers");
    let directory = |variable, name| {
        env::var_os(variable).map(PathBuf::from).unwrap_or_else(|| {
            PathBuf::from(env::var_os("HOME").expect("home or model overrides"))
                .join(".local/share/Agent-Hire/jobs")
                .join(name)
        })
    };
    let runtime = fixture.runtime(ModelDirectories {
        level: directory("AGENT_HIRE_LEVEL_MODEL_DIR", "level-model"),
        department: directory("AGENT_HIRE_DEPARTMENT_MODEL_DIR", "department-model"),
    });
    let db = fixture.index.connect().expect("open job index");
    let job_id = jobindex::add_custom_job(
        &db,
        "Software Engineering Intern",
        "Acme",
        "Must be enrolled in a computer science program.",
        "https://example.test/jobs/123",
        "",
        "",
    )
    .expect("create job");
    runtime.start().expect("start runtime");
    assert_eq!(runtime.device(), "not loaded");
    runtime.notify();
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let progress = runtime.progress().expect("read progress");
        if progress["classified"] == 1 {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "encoders did not finish: {progress}"
        );
        thread::sleep(Duration::from_millis(100));
    }
    runtime.stop();
    assert_eq!(runtime.device(), "cpu");
    let mut statement = db
        .prepare("SELECT job_id,role_key,role_match,role_p,source FROM decisions ORDER BY role_key")
        .expect("read encoder verdicts");
    let rows: Vec<_> = statement
        .query_map([], jobindex::row_object)
        .expect("query decisions")
        .collect::<rusqlite::Result<_>>()
        .expect("read decisions");
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["role_key"], "department:encoder");
    assert_eq!(rows[1]["role_key"], "level:encoder");
    for row in rows {
        assert_eq!(row["job_id"], job_id);
        assert_eq!(row["role_match"], 1);
        assert_eq!(row["source"], "encoder");
        assert!((0.0..=1.0).contains(&row["role_p"].as_f64().expect("encoder probability")));
    }
}
