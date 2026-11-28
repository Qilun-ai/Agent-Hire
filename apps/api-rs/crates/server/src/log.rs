//! One-line JSON logs on stderr, in the same shape the Python backend writes:
//! `{"ts": "...Z", "level": "INFO", "module": "...", "msg": "..."}`.

use std::io;
use std::io::Write;

use serde_json::json;

use agent_hire_store::UtcTime;

pub fn info(module: &str, message: &str) {
    write("INFO", module, message);
}

pub fn warning(module: &str, message: &str) {
    write("WARNING", module, message);
}

fn write(level: &str, module: &str, message: &str) {
    let line = json!({
        "ts": UtcTime::now().to_iso_seconds_z(),
        "level": level,
        "module": module,
        "msg": message,
    });
    // Logging must never take the server down; a closed stderr just drops the line.
    let _ = writeln!(io::stderr().lock(), "{line}");
}
