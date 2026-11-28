//! Deterministic route results, selection scope, and readiness behavior.

use agent_hire_generation::{
    GenerateOptions, GenerateRequest, GenerationError, ProfileScope, Selection, SuggestRequest,
    corpus_hydration_id, generate, hydrate_description, prepare_profile, suggest_selection,
};
use agent_hire_llm::Settings;
use serde_json::json;

#[tokio::test]
async fn fallback_package_returns_the_route_payload_and_versioned_files() {
    let dir = tempfile::tempdir().expect("temporary output directory");
    let lead = json!({"job_id":"backend-1","title":"Backend Engineer","company":"Acme",
        "description":"Rust and Kafka, distributed systems.","outreach_reply":"old","source_meta":{"keep":true}});
    let profile = json!({"n":"Maya","skills":[{"n":"Rust","cat":"language"}],
        "projects":[{"title":"Ledger","stack":["Rust"],"impact":"Shipped reconciliation."}]});
    let settings = Settings::new([("contact_lookup_enabled".to_owned(), "off".to_owned())]);
    let generated = generate(GenerateRequest {
        job_id: "backend-1",
        generation_job_id: "generation-7",
        lead: Some(&lead),
        profile: &profile,
        settings: &settings,
        output_dir: dir.path(),
        current_version: 2,
        options: GenerateOptions::default(),
    })
    .await
    .expect("deterministic generation");
    assert!(generated.files.resume.ends_with("backend-1_v3.pdf"));
    assert!(
        generated
            .files
            .cover_letter
            .ends_with("backend-1_cl_v3.pdf")
    );
    assert!(generated.files.resume.is_file() && generated.files.cover_letter.is_file());
    let response = generated.response(&["contact lookup: database busy".to_owned()]);
    assert_eq!(response["status"], "ready");
    assert_eq!(response["generation_job_id"], "generation-7");
    assert_eq!(response["lead"]["generation_job_id"], "generation-7");
    assert!(
        generated
            .enriched_lead(&[])
            .get("generation_job_id")
            .is_none()
    );
    assert_eq!(response["lead"]["status"], "approved");
    assert_eq!(response["lead"]["asset"], response["lead"]["resume_asset"]);
    assert_eq!(
        response["lead"]["keyword_coverage"]["jd_terms"],
        json!(["Rust", "Kafka", "Distributed Systems"])
    );
    assert_eq!(response["lead"]["keyword_coverage"]["coverage_pct"], 33);
    assert_eq!(response["lead"]["source_meta"]["keep"], true);
    assert_eq!(
        response["lead"]["source_meta"]["generation_persistence_errors"][0],
        "contact lookup: database busy"
    );
    assert_ne!(response["lead"]["outreach_reply"], "old");
}

#[test]
fn selected_points_keep_their_text_and_order_after_tag_and_conflict_scoping() {
    let profile = json!({
        "n":"Maya","skills":[{"n":"Rust","id":"s"},{"n":"Go","id":"g","tag_ids":["other"]}],
        "projects":[{"title":"Ledger","id":"p","points":[
            {"id":"first","text":"First proof."},
            {"id":"other","text":"Other tag.","tag_ids":["other"]},
            {"id":"winner","text":"Second proof;  keep the spacing.","tag_ids":["scope"]},
            {"id":"variant","text":"Unselected variant."}
        ]}],
    });
    let conflicts = [
        json!({"status":"picked","picked_kind":"project","picked_id":"winner",
        "members":[{"point_kind":"project","point_id":"winner"},{"point_kind":"project","point_id":"variant"}]}),
    ];
    let selected = Selection {
        projects_on: vec!["p".to_owned()],
        points_on: vec!["winner".to_owned(), "first".to_owned(), "other".to_owned()],
        skills_on: vec!["s".to_owned()],
        ..Selection::default()
    };
    let prepared = prepare_profile(
        &profile,
        ProfileScope {
            tag_id: "scope",
            conflicts: &conflicts,
            ..ProfileScope::default()
        },
        Some(&selected),
    );
    assert!(prepared.verbatim);
    assert_eq!(prepared.stale_ids, 1);
    assert_eq!(
        prepared.profile["projects"][0]["impact"],
        "Second proof;  keep the spacing.\nFirst proof."
    );
    assert_eq!(
        prepared.profile["skills"].as_array().expect("skills").len(),
        1
    );
    assert_eq!(
        profile["projects"][0]["points"]
            .as_array()
            .expect("original points")
            .len(),
        4
    );
}

#[tokio::test]
async fn validation_and_default_suggestions_work_without_a_provider() {
    let settings = Settings::default();
    let profile = json!({"projects":[{"id":"p","title":"Ledger","stack":["Rust"],
        "points":[{"id":"point","text":"Built a ledger."}]}]});
    let lead = json!({"title":"Rust engineer","description":"See https://example.test/jobs/123",
        "source_meta":{"corpus_job_id":17}});
    assert_eq!(corpus_hydration_id(&lead), Some(&json!(17)));
    let detail = json!({"description_md":"  Design ledger services and monitor pipelines across regions.  "});
    let (hydrated, changed) = hydrate_description(&lead, Some(&detail));
    assert!(changed);
    assert_eq!(hydrated["source_meta"]["description_hydrated"], true);
    assert_eq!(corpus_hydration_id(&hydrated), None);
    let request = SuggestRequest {
        job_id: "j",
        lead: Some(&lead),
        profile: &profile,
        settings: &settings,
        scope: ProfileScope::default(),
        corpus_detail: Some(&detail),
        readiness_blocker: None,
    };
    let response = suggest_selection(request).await.expect("suggestions");
    assert_eq!(response["selection"]["projects_on"], json!(["p"]));
    assert_eq!(response["selection"]["points_on"], json!(["point"]));
    assert_eq!(response["rewrites"], json!([]));
    let error = GenerationError::Transient("transport timed out".to_owned());
    assert_eq!(error.status(), 503);
    assert_eq!(error.retry_after(), Some(30));
    assert_eq!(
        error.body(),
        json!({"detail":"Generation failed. See the activity log for details."})
    );
    assert_eq!(
        error.failed_lead(&lead).expect("failed lead")["status"],
        "tailoring"
    );
}
