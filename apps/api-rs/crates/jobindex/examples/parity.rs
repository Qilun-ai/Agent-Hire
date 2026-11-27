//! Reads `{"id","title","description","location"}` lines on stdin and prints the level, skills,
//! software flag and country codes per line, for comparing with the Python rules.

use std::io;
use std::io::{BufRead, Write};

use agent_hire_jobindex::{
    classify_level, codes_column, extract_skills, rule_is_software, software_skills,
};
use serde_json::{Value, json};

fn main() {
    let stdout = io::stdout();
    let mut out = stdout.lock();
    for line in io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let Ok(row) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let title = row["title"].as_str().unwrap_or("");
        let description = row["description"].as_str().unwrap_or("");
        let level = classify_level(title, description);
        let skills = extract_skills(&format!("{title}\n{description}"));
        let software = rule_is_software(title, &skills, software_skills());
        let result = json!({
            "id": row["id"],
            "level": level.level,
            "years_min": level.years_min,
            "years_max": level.years_max,
            "basis": level.basis,
            "lines": level.lines,
            "skills": skills,
            "software": software,
            "codes": codes_column(row["location"].as_str().unwrap_or(""), None),
        });
        let _ = writeln!(out, "{result}");
    }
}
