//! Renders a synthetic application package into the requested sample directory.

use std::env;
use std::error::Error;
use std::path::PathBuf;

use agent_hire_generation::{GenerateOptions, GenerateRequest, generate};
use agent_hire_llm::Settings;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let output = env::args_os().nth(1).map_or_else(
        || PathBuf::from("/tmp/agent-hire-generation-sample"),
        PathBuf::from,
    );
    let profile = json!({
        "n":"Maya Rao","identity":{"email":"maya.rao@example.test","github_url":"https://github.com/maya-example"},
        "s":"Backend engineer specializing in transaction systems and observability.",
        "skills":[{"n":"Rust","cat":"language"},{"n":"Python","cat":"language"},{"n":"PostgreSQL","cat":"database"},
            {"n":"Kafka","cat":"data"},{"n":"Docker","cat":"tool"},{"n":"Terraform","cat":"infra"}],
        "projects":[
            {"title":"Ledger Reconciler","stack":["Rust","PostgreSQL"],"points":[
                {"text":"Implemented idempotent reconciliation for payment events and published a replay tool for operators."}]},
            {"title":"Event Inspector","stack":["Python","Kafka"],"points":[
                {"text":"Built an event inspection service with schema validation and operator-facing diagnostics."}]}
        ],
        "exp":[
            {"role":"Backend Engineer","co":"Harbor Labs","period":"Jan 2023 - Present","points":[
                {"text":"Designed transaction ingestion and operational alerts for a shared payments platform."},
                {"text":"Documented the rollback process and automated recovery checks for on-call engineers."}]},
            {"role":"Software Engineer","co":"Juniper Works","period":"2020 - 2022","points":[
                {"text":"Built APIs and database migrations for internal reporting and data quality services."}]}
        ],
        "certifications":["AWS Certified Developer – Associate"],
        "education":["B.S. Computer Science, Stonebridge University"]
    });
    let lead = json!({"job_id":"sample-role","title":"Backend Engineer","company":"Aster Payments",
        "description":"Build Rust services for payment reconciliation using PostgreSQL and Kafka. Own observability and operational tooling."});
    let settings = Settings::new([("contact_lookup_enabled".to_owned(), "off".to_owned())]);
    let job = generate(GenerateRequest {
        job_id: "sample-role",
        generation_job_id: "sample-generation",
        lead: Some(&lead),
        profile: &profile,
        settings: &settings,
        output_dir: &output,
        current_version: 0,
        options: GenerateOptions::default(),
    })
    .await?;
    println!("{}", job.files.resume.display());
    println!("{}", job.files.cover_letter.display());
    Ok(())
}
