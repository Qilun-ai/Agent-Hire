//! Predicts both encoder verdicts for the first 300 postings in a JSONL sample.

use std::env;
use std::error::Error;
use std::fs::File;
use std::io;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::time::Instant;

use agent_hire_ml::{Encoder, Job, ModelKind, Prediction};
use serde::{Deserialize, Serialize};

const SAMPLE_SIZE: usize = 300;

#[derive(Deserialize)]
struct SampleRow {
    id: String,
    title: Option<String>,
    description: Option<String>,
    department: Option<String>,
    company: Option<String>,
}

impl SampleRow {
    fn job(&self) -> Job<'_> {
        Job {
            title: self.title.as_deref().unwrap_or_default(),
            description: self.description.as_deref().unwrap_or_default(),
            department: self.department.as_deref().unwrap_or_default(),
            company: self.company.as_deref().unwrap_or_default(),
        }
    }
}

#[derive(Serialize)]
struct OutputRow<'a> {
    id: &'a str,
    level_text: String,
    department_text: String,
    level: &'a Prediction,
    department: &'a Prediction,
}

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let [sample_path, level_dir, department_dir] = args.as_slice() else {
        return Err(
            "usage: ml_parity <sample.jsonl> <level-model-dir> <department-model-dir>".into(),
        );
    };
    let source = BufReader::new(File::open(sample_path)?);
    let mut rows = Vec::with_capacity(SAMPLE_SIZE);
    for line in source.lines().take(SAMPLE_SIZE) {
        let row: SampleRow = serde_json::from_str(&line?)?;
        rows.push(row);
    }
    if rows.len() != SAMPLE_SIZE {
        return Err(format!("expected {SAMPLE_SIZE} postings, got {}", rows.len()).into());
    }

    let jobs: Vec<Job<'_>> = rows.iter().map(SampleRow::job).collect();
    let mut level = Encoder::load(ModelKind::Level, Path::new(level_dir))?;
    let mut department = Encoder::load(ModelKind::Department, Path::new(department_dir))?;
    let levels = timed_predict(&mut level, "level", &jobs)?;
    let departments = timed_predict(&mut department, "department", &jobs)?;
    let mut output = BufWriter::new(io::stdout().lock());
    for (index, row) in rows.iter().enumerate() {
        let prediction = OutputRow {
            id: &row.id,
            level_text: level.build_text(&jobs[index]),
            department_text: department.build_text(&jobs[index]),
            level: &levels[index],
            department: &departments[index],
        };
        serde_json::to_writer(&mut output, &prediction)?;
        writeln!(&mut output)?;
    }
    output.flush()?;
    Ok(())
}

fn timed_predict(
    encoder: &mut Encoder,
    name: &str,
    jobs: &[Job<'_>],
) -> Result<Vec<Prediction>, agent_hire_ml::Error> {
    let start = Instant::now();
    let verdicts = encoder.predict(jobs)?;
    let ms_per_job = start.elapsed().as_secs_f64() * 1000.0 / jobs.len() as f64;
    eprintln!("{name}: {ms_per_job:.3} ms/job (CPU)");
    Ok(verdicts)
}
