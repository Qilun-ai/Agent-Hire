//! The department taxonomy (38 subcategories in 17 departments) used by the department model.

use std::collections::BTreeSet;
use std::path::Path;

use serde_json::{Value, json};

/// The decisions key the department model stores its verdicts under.
pub const DEPARTMENT_ROLE_KEY: &str = "department:encoder";
/// Verdicts below this probability are not trusted.
pub const DEPARTMENT_MIN_CONFIDENCE: f64 = 0.3;
/// The decisions key the level model stores its verdicts under.
pub const LEVEL_ROLE_KEY: &str = "level:encoder";
/// Level verdicts below this probability are not trusted.
pub const LEVEL_MIN_CONFIDENCE: f64 = 0.6;

/// `(department, [(code, name)])`, in display order.
pub const TAXONOMY: &[(&str, &[(&str, &str)])] = &[
    (
        "Technology",
        &[
            ("ENG", "Engineering"),
            ("SWE", "Software Development"),
            ("IT", "Information Technology"),
            ("DATA", "Data and Analytics"),
        ],
    ),
    (
        "Design and Creative",
        &[("DES", "Design"), ("ART", "Creative and Art Services")],
    ),
    (
        "Business Operations",
        &[
            ("PGM", "Project and Program Management"),
            ("PM", "Product Management"),
            ("OPS", "Business Operations"),
            ("LEGAL", "Legal and Compliance"),
            ("FIN", "Finance and Accounting"),
            ("HR", "Human Resources"),
            ("ADMIN", "Administrative & Clerical Support"),
        ],
    ),
    (
        "Sales and Marketing",
        &[
            ("SALES", "Sales"),
            ("MKT", "Marketing"),
            ("COMMS", "Communications and Public Affairs"),
            ("BD", "Business Development"),
        ],
    ),
    (
        "Healthcare",
        &[
            ("HC_ADV", "Healthcare Services - Advanced Practice"),
            ("HC_ALLIED", "Healthcare Services - Allied Health"),
            ("HC_NURSE", "Healthcare Services - Nursing"),
            ("HC_PHARM", "Healthcare Services - Pharmacy"),
            ("HC_VET", "Healthcare Services - Veterinary"),
        ],
    ),
    ("Education", &[("EDU", "Education services")]),
    (
        "Customer and Social Services",
        &[("CS", "Customer Service"), ("SOCIAL", "Social Services")],
    ),
    (
        "Skilled Trades",
        &[
            ("T_CONSTR", "Skilled Trades - Construction"),
            ("T_MECHEL", "Skilled Trades - Mechanical and Electrical"),
            ("T_MFG", "Skilled Trades - Manufacturing and Industrial"),
            ("T_MAINT", "Skilled Trades - Maintenance and Repair"),
            ("T_LABOR", "Skilled Trades - General Labor"),
        ],
    ),
    (
        "Transportation and Logistics",
        &[
            ("TRANSPORT", "Transportation Services"),
            ("SCM", "Supply Chain / Logistics / Procurement"),
        ],
    ),
    (
        "Quality and Safety",
        &[
            ("QA", "Quality Assurance"),
            ("EHS", "Environment, Health, and Safety"),
        ],
    ),
    (
        "Research and Development",
        &[("RND", "Research and Development (R&D)")],
    ),
    (
        "Food and Hospitality",
        &[("FOOD", "Food and Beverage Services")],
    ),
    ("Protective Services", &[("PROTECT", "Protective Services")]),
    ("Custodial Services", &[("CUSTODIAL", "Custodial Services")]),
];

const ALIASES: &[(&str, &str)] = &[
    ("software", "SWE"),
    ("software engineering", "SWE"),
    ("software development", "SWE"),
    ("swe", "SWE"),
    ("it", "IT"),
    ("information technology", "IT"),
    ("data", "DATA"),
    ("data science", "DATA"),
    ("analytics", "DATA"),
    ("engineering", "ENG"),
    ("hardware", "ENG"),
    ("design", "DES"),
    ("product", "PM"),
    ("product management", "PM"),
    ("sales", "SALES"),
    ("marketing", "MKT"),
    ("finance", "FIN"),
    ("accounting", "FIN"),
    ("hr", "HR"),
    ("legal", "LEGAL"),
    ("customer service", "CS"),
    ("customer support", "CS"),
    ("operations", "OPS"),
    ("research", "RND"),
    ("r d", "RND"),
    ("nursing", "HC_NURSE"),
    ("logistics", "SCM"),
    ("supply chain", "SCM"),
    ("qa", "QA"),
    ("quality", "QA"),
];

/// The subcategory's name and department.
pub fn subcategory(code: &str) -> Option<(&'static str, &'static str)> {
    TAXONOMY.iter().find_map(|(department, group)| {
        group
            .iter()
            .find(|(known, _)| *known == code)
            .map(|(_, name)| (*name, *department))
    })
}

/// `{"code", "name", "department"}` for a code.
pub fn describe(code: &str) -> Option<Value> {
    subcategory(code)
        .map(|(name, department)| json!({"code": code, "name": name, "department": department}))
}

fn fold(text: &str) -> String {
    let lowered = text.to_lowercase();
    let mut out = String::new();
    let mut gap = false;
    for character in lowered.chars() {
        if character.is_ascii_lowercase() || character.is_ascii_digit() {
            if gap && !out.is_empty() {
                out.push(' ');
            }
            gap = false;
            out.push(character);
        } else {
            gap = true;
        }
    }
    out
}

/// Maps terms (codes, subcategory names, department names, common words) to codes; also returns
/// the terms that matched nothing.
pub fn resolve(terms: &[String]) -> (BTreeSet<String>, Vec<String>) {
    let mut codes = BTreeSet::new();
    let mut unknown = Vec::new();
    for term in terms {
        let raw = term.trim();
        let key = fold(raw);
        let upper = raw.to_uppercase();
        if subcategory(&upper).is_some() {
            codes.insert(upper);
        } else if let Some(code) = TAXONOMY
            .iter()
            .flat_map(|(_, group)| group.iter())
            .find(|(_, name)| fold(name) == key)
        {
            codes.insert(code.0.to_owned());
        } else if let Some((_, group)) = TAXONOMY
            .iter()
            .find(|(department, _)| fold(department) == key)
        {
            codes.extend(group.iter().map(|(code, _)| (*code).to_owned()));
        } else if let Some((_, code)) = ALIASES.iter().find(|(alias, _)| *alias == key) {
            codes.insert((*code).to_owned());
        } else if !key.is_empty() {
            unknown.push(raw.to_owned());
        }
    }
    (codes, unknown)
}

/// Whether a model folder holds `labels.json`, `tokenizer.json` and the model file it names.
pub fn model_available(dir: &Path) -> bool {
    let labels = dir.join("labels.json");
    let name = std::fs::read_to_string(&labels)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value["model_file"].as_str().map(str::to_owned))
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "model.onnx".to_owned());
    let file_name = Path::new(&name)
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_default();
    labels.exists() && dir.join("tokenizer.json").exists() && dir.join(file_name).exists()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_terms() {
        let terms: Vec<String> = [
            "SWE",
            "Information Technology",
            "Technology",
            "software",
            "nonsense",
        ]
        .iter()
        .map(|t| (*t).to_owned())
        .collect();
        let (codes, unknown) = resolve(&terms);
        assert!(
            codes.contains("SWE")
                && codes.contains("IT")
                && codes.contains("DATA")
                && codes.contains("ENG")
        );
        assert_eq!(unknown, ["nonsense"]);
        assert_eq!(fold("R&D"), "r d");
    }
}
