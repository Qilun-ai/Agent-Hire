//! Prints MiniLM embeddings for each stdin line as JSON lines, for parity checks against a reference.
//!
//! Usage: `cargo run -p agent-hire-ml --example embed_parity -- <model dir> < texts.txt`

use std::env;
use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use agent_hire_ml::Embedder;

fn main() -> io::Result<()> {
    let model_dir: PathBuf = env::args_os()
        .nth(1)
        .map(PathBuf::from)
        .expect("pass the MiniLM model directory as the first argument");
    let texts: Vec<String> = io::stdin().lock().lines().collect::<io::Result<_>>()?;
    let embedder = Embedder::from_model_dir(&model_dir, "onnx", None);
    let batch = embedder.embed_with_mode(&texts);
    let mut out = io::stdout().lock();
    writeln!(
        out,
        "{}",
        serde_json::json!({ "mode": format!("{:?}", batch.mode) })
    )?;
    for vector in batch.vectors {
        writeln!(out, "{}", serde_json::Value::from(vector))?;
    }
    Ok(())
}
