//! Runtime integration checks with an isolated job index and settings database.

use std::fs;
use std::path::PathBuf;

use agent_hire_jobindex::decision::DecisionClient;
use agent_hire_jobindex::decision_store::{self, QueuedVerdict};
use agent_hire_jobindex::kaggle::{self, KaggleManager};
use agent_hire_jobindex::runs::{self, TickMode};
use agent_hire_jobindex::{self as jobindex, JobIndex, ModelDirectories, RULES_VERSION};
use agent_hire_store::{Database, SettingsStore};
use rusqlite::{Connection, params};
use serde_json::{Value, json};

struct Fixture {
    dir: PathBuf,
    index: JobIndex,
    settings: SettingsStore,
}

impl Fixture {
    fn new(test: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "agent-hire-jobindex-runtime-{}-{test}",
            std::process::id()
        ));
        fs::create_dir(&dir).expect("create a private fixture directory");
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

    fn save(&self, settings: &[(&str, &str)]) {
        let settings: Vec<(String, String)> = settings
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        self.settings
            .save(&settings)
            .expect("save fixture settings");
    }

    fn manager(&self) -> KaggleManager {
        KaggleManager::new(
            self.settings.clone(),
            ModelDirectories::from_jobs_dir(&self.dir),
        )
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.dir).expect("remove fixture directory");
    }
}

#[test]
fn migrates_kaggle_settings_and_normalizes_preferences() {
    let fixture = Fixture::new("settings");
    let manager = fixture.manager();
    assert_eq!(
        manager.refresh(false),
        json!({
            "connected":false,"enabled":false,"encoder_device":"auto","has_link":false,"phase":null,"ready":false,
        })
    );
    let old = "https://example.test/notebook?token=sample-token&other=2";
    fixture.save(&[
        ("kaggle_server_url", old),
        ("kaggle_server_key", "old-key"),
        ("encoder_device", "invalid"),
    ]);
    manager.migrate_legacy_settings().expect("migrate settings");
    assert_eq!(
        fixture
            .settings
            .get(kaggle::KEY_LINK, "")
            .expect("read link"),
        old
    );
    assert_eq!(
        fixture
            .settings
            .get("kaggle_server_key", "!")
            .expect("read legacy key"),
        ""
    );
    assert_eq!(manager.public_status()["enabled"], true);
    assert_eq!(manager.encoder_device(), "auto");
    fixture.save(&[(
        "kaggle_server_url",
        "https://example.test/?token=replacement",
    )]);
    manager.migrate_legacy_settings().expect("repeat migration");
    assert_eq!(
        fixture
            .settings
            .get(kaggle::KEY_LINK, "")
            .expect("read link"),
        old
    );
    assert_eq!(
        manager.set_encoder_device("LOCAL").expect("set device")["encoder_device"],
        "local"
    );
    let invalid = manager
        .set_encoder_device("gpu")
        .expect_err("reject invalid mode");
    assert_eq!(invalid.status(), 400);
    assert_eq!(
        invalid.body(),
        json!({"detail":"device must be auto, kaggle or local"})
    );
    assert_eq!(
        manager
            .connect("http://example.test/?token=fake")
            .expect_err("reject non-HTTPS link")
            .status(),
        422
    );
    assert_eq!(manager.forget().expect("forget")["has_link"], false);
    assert_eq!(manager.toggle(false).expect("disable")["enabled"], false);
    assert_eq!(
        manager.stop().expect("stop without a saved link"),
        json!({
            "stopped":false,"error":"No Kaggle link yet — paste one in the Kaggle box at the top right."
        })
    );
}

#[test]
fn imports_completed_companies_and_finalizes_exited_runs() {
    let fixture = Fixture::new("tick");
    let working = fixture.dir.join("working");
    fs::create_dir_all(working.join("companies")).expect("create fake scrape output");
    let source = Connection::open(working.join("jobs.sqlite3")).expect("scrape result database");
    source
        .execute_batch("CREATE TABLE jobs(company TEXT, data TEXT)")
        .expect("schema");
    let record = json!({
        "record_id":"a1","company":"Acme","title":"Senior Backend Engineer",
        "description_text":"Requires 5 years of professional experience with Python.",
        "locations":["Toronto, Canada"],
    });
    source
        .execute(
            "INSERT INTO jobs VALUES (?1,?2)",
            params!["Acme", record.to_string()],
        )
        .expect("job");
    drop(source);
    fs::write(
        working.join("companies/acme.json"),
        r#"{"company":"Acme","status":"complete"}"#,
    )
    .expect("company report");
    fs::write(working.join("scrape.log"), "kept").expect("scraper log");

    let connection = fixture.index.connect().expect("index database");
    connection
        .execute(
            "INSERT INTO runs(id,kind,status,started_at,companies,pid,output_dir)
         VALUES('test','manual','running','2026-01-01T00:00:00+00:00','[\"acme\",\"other\"]',0,?1)",
            [working.to_string_lossy().as_ref()],
        )
        .expect("running scrape");
    let mut notifications = 0;
    runs::tick(
        &fixture.index,
        &fixture.settings,
        &fixture.dir,
        TickMode::ImportOnly,
        || {
            notifications += 1;
            let other = fixture.index.connect().expect("read committed imports");
            assert_eq!(jobindex::stats(&other).expect("counts")["jobs_active"], 1);
        },
    )
    .expect("import completed company");
    assert_eq!(notifications, 1);
    assert!(
        runs::active_run(&connection)
            .expect("read active run")
            .is_some()
    );
    connection.execute(
        "UPDATE jobs SET rules_version=0,rule_software=0,rule_level=NULL,country_codes='' WHERE id='a1'", [],
    ).expect("make rules stale");
    runs::tick(
        &fixture.index,
        &fixture.settings,
        &fixture.dir,
        TickMode::Periodic,
        || notifications += 1,
    )
    .expect("finalize and recompute");
    assert_eq!(notifications, 1);
    let row = connection
        .query_row(
            "SELECT status,imported,seen_jobs FROM runs WHERE id='test'",
            [],
            jobindex::row_object,
        )
        .expect("read finished run");
    assert_eq!(row["status"], "finished_partial");
    assert_eq!(row["imported"], "[\"acme\"]");
    assert_eq!(row["seen_jobs"], 1);
    let row = connection
        .query_row("SELECT * FROM jobs WHERE id='a1'", [], jobindex::row_object)
        .expect("imported job");
    assert_eq!(row["rules_version"], RULES_VERSION);
    assert_eq!(row["rule_software"], 1);
    assert_eq!(row["rule_level"], "senior");
    assert!(
        row["country_codes"]
            .as_str()
            .expect("country code")
            .contains("CA")
    );
    assert!(working.join("scrape.log").exists());
    assert!(!working.join("jobs.sqlite3").exists());

    connection
        .execute(
            "INSERT INTO runs(id,kind,status,started_at,companies,pid)
         VALUES('orphan','manual','running','2026-01-01T00:00:00+00:00','[]',0)",
            [],
        )
        .expect("orphan");
    runs::interrupt_orphans(&fixture.index, || {}).expect("interrupt orphan");
    assert_eq!(
        runs::run_status(&fixture.index, Some("orphan"), 12.0).expect("status")["run"]["status"],
        "interrupted"
    );
}

#[test]
fn queues_newest_active_postings_and_skips_superseded_verdicts() {
    let fixture = Fixture::new("queue");
    let db = fixture.index.connect().expect("index database");
    db.execute_batch(
        "INSERT INTO jobs(id,company,company_slug,title,first_seen_at,last_seen_at,content_hash)
         VALUES('older','X','x','Engineer','2026-01-01','2026-01-01','old1'),
               ('newer','X','x','Engineer','2026-01-02','2026-01-02','new1'),
               ('closed','X','x','Engineer','2026-01-03','2026-01-03','closed1');
         UPDATE jobs SET active=0 WHERE id='closed';",
    )
    .expect("job fixtures");
    let role = jobindex::LEVEL_ROLE_KEY;
    let rows = decision_store::pending_jobs(&fixture.index, role, 8).expect("queued jobs");
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(ids, ["newer", "older"]);
    db.execute("UPDATE jobs SET content_hash='new2' WHERE id='newer'", [])
        .expect("revise job during inference");
    let verdicts: Vec<QueuedVerdict<'_>> = rows
        .iter()
        .map(|job| QueuedVerdict {
            job,
            role_match: true,
            probability: Some(0.6543),
            label: Some("mid"),
        })
        .collect();
    assert_eq!(
        decision_store::save_verdicts(&fixture.index, role, "encoder", &verdicts)
            .expect("write verdicts"),
        1
    );
    let progress =
        decision_store::encoder_progress(&db, true, false, "cpu").expect("encoder progress");
    assert_eq!(
        progress,
        json!({
            "active":2,"classified":0,"department_done":0,"level_done":1,"remaining":2,
            "models":{"department":false,"level":true},"device":"cpu",
        })
    );
    let source: String = db
        .query_row(
            "SELECT source FROM decisions WHERE job_id='older'",
            [],
            |row| row.get(0),
        )
        .expect("saved source");
    assert_eq!(source, "encoder");
}

#[test]
#[ignore = "uses a local TCP listener to verify the System One wire protocol"]
fn decision_http_probe_uses_expected_wire_format() {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    let fixture = Fixture::new("http");
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock server");
    let url = format!("http://{}/v1/", listener.local_addr().expect("mock URL"));
    fixture.save(&[
        ("decision_api_url", &url),
        ("decision_api_model", "nimble"),
        ("decision_api_key", "secret-for-test"),
    ]);
    let client = DecisionClient::new(fixture.settings.clone(), fixture.manager());
    let endpoint = client
        .endpoints_from_settings(false)
        .into_iter()
        .next()
        .expect("configured endpoint");
    assert!(endpoint.systemone_url().ends_with("/v1/systemone"));
    assert!(endpoint.health_url().ends_with("/health"));
    let mock = thread::spawn(move || {
        let (mut socket, _) = listener.accept().expect("accept decision probe");
        socket
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set read deadline");
        let mut reader = BufReader::new(&socket);
        let mut line = String::new();
        reader.read_line(&mut line).expect("read request line");
        assert_eq!(line, "POST /v1/systemone HTTP/1.1\r\n");
        let mut body_length = None;
        let mut authenticated = false;
        loop {
            line.clear();
            let bytes = reader.read_line(&mut line).expect("read request headers");
            if bytes == 0 || line.trim_end().is_empty() {
                break;
            }
            if let Some((key, value)) = line.trim_end().split_once(':') {
                match key.to_ascii_lowercase().as_str() {
                    "content-length" => {
                        body_length = Some(value.trim().parse().expect("body length"))
                    }
                    "authorization" => authenticated = value.trim() == "Bearer secret-for-test",
                    _ => (),
                }
            }
        }
        assert!(
            authenticated,
            "decision request authenticates with its configured key"
        );
        let mut body = vec![0; body_length.expect("request body length")];
        reader.read_exact(&mut body).expect("request body");
        let body: Value = serde_json::from_slice(&body).expect("valid JSON body");
        assert_eq!(body["model"], "nimble");
        assert_eq!(body["questions"]["role"]["type"], "noul");
        let body = r#"{"answers":{"role":{"noul":0.91},"experience":{"choice":"mid"}}}"#;
        write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
            .expect("mock response");
    });
    assert_eq!(
        client.probe(&endpoint),
        json!({"ok":true,"endpoint":"decision_api","role_p":0.91,"level":"mid"})
    );
    mock.join().expect("mock server handled probe");
}

#[test]
#[ignore = "requires AGENT_HIRE_TEST_KAGGLE_LINK for a live Jupyter status request"]
fn kaggle_reads_notebook_state() {
    let fixture = Fixture::new("notebook");
    let link =
        std::env::var("AGENT_HIRE_TEST_KAGGLE_LINK").expect("configure the test notebook link");
    fixture.save(&[(kaggle::KEY_LINK, &link)]);
    let manager = fixture.manager();
    let status = manager.refresh(true);
    assert_eq!(status["connected"], true, "{status}");
    assert_eq!(status["has_link"], true);
}
