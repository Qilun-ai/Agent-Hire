//! Smoke tests for the deployed 4-bit encoder models.

use std::env;
use std::path::PathBuf;

use agent_hire_ml::{Encoder, Job, ModelKind};

#[test]
#[ignore = "requires the shipped model directories"]
fn quantized_models_run_on_cpu() {
    let home = env::var_os("HOME").map(PathBuf::from);
    let jobs = [
        Job {
            title: "Software Engineering Intern",
            description: "Requirements: Must be enrolled in a computer science degree.",
            company: "Example",
            department: "Engineering",
        },
        Job {
            title: "Software Engineer",
            description: "Build backend services. Requires 3+ years of professional experience.",
            ..Job::default()
        },
    ];

    for (kind, variable, folder) in [
        (
            ModelKind::Level,
            "AGENT_HIRE_LEVEL_MODEL_DIR",
            "level-model",
        ),
        (
            ModelKind::Department,
            "AGENT_HIRE_DEPARTMENT_MODEL_DIR",
            "department-model",
        ),
    ] {
        let directory = env::var_os(variable).map(PathBuf::from).unwrap_or_else(|| {
            home.as_ref()
                .expect("HOME or an explicit model directory is required")
                .join(".local/share/Agent-Hire/jobs")
                .join(folder)
        });
        let mut model = Encoder::load(kind, &directory)
            .expect("the shipped quantized model must load with the CPU provider");
        let predictions = model
            .predict(&jobs)
            .expect("the shipped quantized model must run on CPU");
        assert_eq!(
            predictions.len(),
            jobs.len(),
            "expected a prediction for every {folder} input"
        );
        for prediction in predictions {
            assert!(
                !prediction.label.is_empty(),
                "{folder} returned an empty label"
            );
            assert!(
                (0.0..=1.0).contains(&prediction.probability),
                "{folder} returned an invalid probability: {prediction:?}"
            );
        }
    }
}
