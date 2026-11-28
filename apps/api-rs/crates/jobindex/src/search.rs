//! The agent's job search: index candidates, level and department verdicts, skill-overlap ranking,
//! one compact page.
//!
//! The index can hold hundreds of thousands of jobs; the agent only ever receives a counted
//! summary and one page without descriptions, and asks for single jobs when it needs them.

use std::collections::{HashMap, HashSet};

use rusqlite::{Connection, params_from_iter};
use serde_json::{Map, Value, json};
use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::is_combining_mark;

use crate::datetime::DateTime;
use crate::departments::{self, DEPARTMENT_MIN_CONFIDENCE, LEVEL_MIN_CONFIDENCE};
use crate::model_directories::ModelDirectories;
use crate::store::{self, JobIndex, row_object};
use crate::{geo, rules, skills};

/// The most jobs one page may hold.
pub const PAGE_MAX: i64 = 25;

/// What to search for.
#[derive(Clone, Debug, PartialEq)]
pub struct SearchParams {
    /// A free-text role ("backend engineer"); a known family switches to its rule.
    pub role: String,
    /// Full-text keywords (any).
    pub keywords: Vec<String>,
    /// Posted (or first seen) within this many hours.
    pub since_hours: Option<f64>,
    /// Wanted levels, in user words.
    pub levels: Vec<String>,
    /// Department codes, names or words.
    pub departments: Vec<String>,
    /// Places; a country also matches its cities.
    pub locations: Vec<String>,
    /// Company names or slugs (substring).
    pub companies: Vec<String>,
    /// Exact company slugs.
    pub company_slugs: Vec<String>,
    /// Whole words the title must have (any); `-word` excludes.
    pub title_keywords: Vec<String>,
    /// Only remote jobs.
    pub remote: Option<bool>,
    /// Only jobs sharing a skill with the profile.
    pub match_profile: bool,
    /// `match`, `recent` or `level`.
    pub sort: String,
    /// 1-based page.
    pub page: i64,
    /// Jobs per page (at most 25).
    pub page_size: i64,
}

/// Normalised skills from the profile: listed skills plus vocabulary skills found in experience
/// and project text.
pub fn profile_skill_set(profile: &Map<String, Value>) -> HashSet<String> {
    let mut out = HashSet::new();
    for item in profile
        .get("skills")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let name = match item {
            Value::Object(map) => map
                .get("n")
                .filter(|v| truthy(v))
                .or_else(|| map.get("name"))
                .cloned(),
            other => Some(other.clone()),
        };
        if let Some(name) = name.filter(truthy) {
            let text = match name {
                Value::String(text) => text,
                other => other.to_string(),
            };
            let normalized = skills::normalize_skill(&text);
            if !normalized.is_empty() {
                out.insert(normalized);
            }
        }
    }
    let mut parts: Vec<String> = Vec::new();
    let sections = ["exp", "projects"].into_iter().flat_map(|key| {
        profile
            .get(key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    });
    for item in sections {
        let Some(map) = item.as_object() else {
            continue;
        };
        for key in [
            "role",
            "title",
            "name",
            "d",
            "desc",
            "description",
            "stack",
            "s",
        ] {
            match map.get(key) {
                Some(Value::String(text)) => parts.push(text.clone()),
                Some(Value::Array(items)) => parts.extend(items.iter().map(python_str)),
                _ => {}
            }
        }
        for key in ["bullets", "points", "highlights"] {
            for point in map.get(key).and_then(Value::as_array).into_iter().flatten() {
                parts.push(match point {
                    Value::Object(inner) => inner
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    other => python_str(other),
                });
            }
        }
    }
    out.extend(
        skills::extract_skills(&parts.join("\n"))
            .into_iter()
            .take(200),
    );
    out
}

fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_owned(),
        Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
        other => other.to_string(),
    }
}

fn truthy(value: &Value) -> bool {
    !matches!(value, Value::Null | Value::Bool(false))
        && value.as_str() != Some("")
        && value.as_i64() != Some(0)
}

/// Lower case without accents ("Sênior" → "senior").
pub fn fold(text: &str) -> String {
    text.nfkd()
        .filter(|c| !is_combining_mark(*c))
        .collect::<String>()
        .to_lowercase()
}

fn title_terms(raw: &[String]) -> (Vec<String>, Vec<String>) {
    let mut include = Vec::new();
    let mut exclude = Vec::new();
    for term in raw {
        let term = fold(term.split_whitespace().collect::<Vec<_>>().join(" ").trim());
        if let Some(rest) = term
            .strip_prefix('-')
            .map(str::trim)
            .filter(|rest| !rest.is_empty())
        {
            exclude.push(rest.to_owned());
        } else if !term.is_empty() && term != "-" {
            include.push(term);
        }
    }
    (include, exclude)
}

fn is_word_char(c: Option<char>) -> bool {
    c.is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

/// Whether `term` occurs in `text` (both lower case) as a whole word; with `left_only`, only the
/// left edge must be a boundary.
fn has_word(text: &str, term: &str, left_only: bool) -> bool {
    if term.is_empty() {
        return true;
    }
    text.match_indices(term).any(|(start, _)| {
        let before = text[..start].chars().next_back();
        let after = text[start + term.len()..].chars().next();
        !is_word_char(before) && (left_only || !is_word_char(after))
    })
}

fn fts_query(words: &[String]) -> String {
    let terms: Vec<String> = words
        .iter()
        .map(|word| skills::tokens(&word.replace('"', " ")).join(" "))
        .filter(|cleaned| !cleaned.is_empty())
        .map(|cleaned| format!("\"{}\"", cleaned.replace('"', "")))
        .collect();
    terms.join(" OR ")
}

/// The filters the SQL prefilter applies.
struct CandidateFilters<'a> {
    family: Option<&'a str>,
    role_words: &'a [String],
    title_any: &'a [String],
}

fn candidates(
    connection: &Connection,
    params: &SearchParams,
    filters: &CandidateFilters<'_>,
) -> rusqlite::Result<Vec<Map<String, Value>>> {
    let mut clauses = vec!["j.active=1".to_owned()];
    let mut values: Vec<String> = Vec::new();
    if let Some(hours) = params.since_hours.filter(|hours| *hours > 0.0) {
        let cutoff = DateTime::now_utc()
            .minus_seconds((hours * 3600.0) as i64)
            .isoformat_seconds();
        clauses.push(format!("{} >= ?", store::posted_expr("j")));
        values.push(cutoff);
    }
    if filters.family == Some("software") {
        clauses.push("j.rule_software=1".to_owned());
    } else if !filters.role_words.is_empty() {
        clauses.push(format!(
            "({})",
            vec!["lower(j.title) LIKE ?"; filters.role_words.len()].join(" AND ")
        ));
        values.extend(
            filters
                .role_words
                .iter()
                .map(|word| format!("%{}%", word.to_lowercase())),
        );
    }
    if !params.locations.is_empty() {
        let mut parts = Vec::new();
        for place in &params.locations {
            match geo::country_of(place) {
                Some(code) => {
                    parts.push("(j.country_codes LIKE ? OR lower(j.location_text) LIKE ?)");
                    values.push(format!("%,{code},%"));
                    values.push(format!("%{}%", place.to_lowercase()));
                }
                None => {
                    parts.push("lower(j.location_text) LIKE ?");
                    values.push(format!("%{}%", place.to_lowercase()));
                }
            }
        }
        clauses.push(format!("({})", parts.join(" OR ")));
    }
    if !params.companies.is_empty() {
        clauses.push(format!(
            "({})",
            vec!["(j.company_slug=? OR lower(j.company) LIKE ?)"; params.companies.len()]
                .join(" OR ")
        ));
        for company in &params.companies {
            values.push(company.to_lowercase());
            values.push(format!("%{}%", company.to_lowercase()));
        }
    }
    if !params.company_slugs.is_empty() {
        clauses.push(format!(
            "j.company_slug IN ({})",
            vec!["?"; params.company_slugs.len()].join(",")
        ));
        values.extend(params.company_slugs.iter().cloned());
    }
    if !filters.title_any.is_empty() {
        clauses.push(format!(
            "({})",
            vec!["lower(j.title) LIKE ?"; filters.title_any.len()].join(" OR ")
        ));
        values.extend(
            filters
                .title_any
                .iter()
                .map(|term| format!("%{}%", term.to_lowercase())),
        );
    }
    if params.remote == Some(true) {
        clauses.push("(lower(COALESCE(j.workplace_type,'')) LIKE '%remote%' OR lower(j.location_text) LIKE '%remote%')".to_owned());
    }
    if !params.keywords.is_empty() {
        let query = fts_query(&params.keywords);
        if !query.is_empty() {
            clauses
                .push("j.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)".to_owned());
            values.push(query);
        }
    }
    let sql = format!(
        "SELECT j.id, j.company, j.company_slug, j.title, j.location_text, j.workplace_type, j.skills, \
         j.rule_level, j.min_years, j.date_posted, j.posted_source, j.first_seen_at, j.baseline, j.url, \
         j.apply_url, j.lead_id, lv.level AS enc_level, lv.role_p AS enc_level_p, dp.level AS dept_code, dp.role_p AS dept_p \
         FROM jobs j \
         LEFT JOIN decisions lv ON lv.job_id = j.id AND lv.role_key = 'level:encoder' \
         LEFT JOIN decisions dp ON dp.job_id = j.id AND dp.role_key = 'department:encoder' \
         WHERE {} ORDER BY {} DESC LIMIT 50000",
        clauses.join(" AND "),
        store::posted_expr("j")
    );
    let mut statement = connection.prepare(&sql)?;
    let rows = statement.query_map(params_from_iter(values), row_object)?;
    rows.collect()
}

fn text<'a>(row: &'a Map<String, Value>, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or("")
}

fn number(row: &Map<String, Value>, key: &str) -> f64 {
    row.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

/// One match: the row, its level, the profile skills it shares, its skills and the verdict.
struct Match {
    row: Map<String, Value>,
    level: Option<String>,
    overlap: Vec<String>,
    job_skills: Vec<String>,
    verdict: Map<String, Value>,
}

impl Match {
    fn recency(&self) -> String {
        let posted = text(&self.row, "date_posted");
        if !posted.is_empty() {
            return posted.to_owned();
        }
        if number(&self.row, "baseline") == 0.0 {
            text(&self.row, "first_seen_at").to_owned()
        } else {
            String::new()
        }
    }
}

/// Runs a search and returns the page, facets and counts.
pub fn run(
    index: &JobIndex,
    params: &SearchParams,
    profile: &Map<String, Value>,
) -> rusqlite::Result<Value> {
    let family = rules::role_family(&params.role);
    let role_words: Vec<String> = skills::tokens(&params.role)
        .into_iter()
        .filter(|word| {
            word.chars().count() > 2
                && !matches!(word.as_str(), "and" | "the" | "role" | "job" | "jobs")
        })
        .collect();
    let levels: Vec<&'static str> = params
        .levels
        .iter()
        .filter_map(|level| rules::normalize_level(level))
        .collect();
    let (title_include, title_exclude) = title_terms(&params.title_keywords);
    let connection = index.connect()?;
    let no_words: Vec<String> = Vec::new();
    let filters = CandidateFilters {
        family,
        role_words: if family.is_some() {
            &no_words
        } else {
            &role_words
        },
        title_any: &title_include,
    };
    let mut rows = candidates(&connection, params, &filters)?;
    let stats = store::stats_cached(index, &connection)?;
    if !title_include.is_empty() || !title_exclude.is_empty() {
        rows.retain(|row| {
            let title = fold(text(row, "title"));
            (title_include.is_empty()
                || title_include
                    .iter()
                    .any(|term| has_word(&title, term, false)))
                && !title_exclude
                    .iter()
                    .any(|term| has_word(&title, term, false))
        });
    }
    let role_key = if params.role.is_empty() {
        String::new()
    } else if let Some(family) = family {
        format!("family:{family}")
    } else {
        let joined = skills::tokens(&params.role).join(" ");
        format!("role:{}", joined.chars().take(80).collect::<String>())
    };
    let mut decided = Map::new();
    let mut decision_source = "rules".to_owned();
    if !role_key.is_empty() {
        let ids: Vec<String> = rows.iter().map(|row| text(row, "id").to_owned()).collect();
        decided = store::decisions_for(&connection, &role_key, &ids)?;
        if let Some(first) = decided.values().next() {
            decision_source = first["source"].as_str().unwrap_or("rules").to_owned();
        }
    }
    let encoder: HashMap<String, String> = rows
        .iter()
        .filter(|row| {
            !text(row, "enc_level").is_empty() && number(row, "enc_level_p") >= LEVEL_MIN_CONFIDENCE
        })
        .map(|row| {
            (
                text(row, "id").to_owned(),
                text(row, "enc_level").to_owned(),
            )
        })
        .collect();
    let (dept_codes, dept_unknown) = departments::resolve(&params.departments);
    let dept_model =
        departments::model_available(&ModelDirectories::from_jobs_dir(index.dir()).department);
    let department_of: HashMap<String, (String, f64)> = rows
        .iter()
        .filter(|row| {
            departments::subcategory(text(row, "dept_code")).is_some()
                && number(row, "dept_p") >= DEPARTMENT_MIN_CONFIDENCE
        })
        .map(|row| {
            (
                text(row, "id").to_owned(),
                (text(row, "dept_code").to_owned(), number(row, "dept_p")),
            )
        })
        .collect();
    let profile_skills = if params.match_profile || params.sort == "match" {
        profile_skill_set(profile)
    } else {
        HashSet::new()
    };
    let free_text_role = !params.role.is_empty() && family.is_none();
    let mut matches: Vec<Match> = Vec::new();
    for row in &rows {
        let title = text(row, "title").to_lowercase();
        if free_text_role
            && !role_words
                .iter()
                .all(|word| has_word(&title, &word.to_lowercase(), true))
        {
            continue;
        }
        let job_skills: Vec<String> = serde_json::from_str(text(row, "skills")).unwrap_or_default();
        let id = text(row, "id");
        let verdict = decided.get(id).and_then(Value::as_object);
        if !params.role.is_empty()
            && verdict.is_some_and(|v| !v.get("role_match").is_some_and(truthy))
        {
            continue;
        }
        let verdict_level = verdict
            .and_then(|v| v.get("level"))
            .and_then(Value::as_str)
            .filter(|l| !l.is_empty());
        let rule_level = Some(text(row, "rule_level")).filter(|l| !l.is_empty());
        let level = encoder
            .get(id)
            .map(String::as_str)
            .or(rule_level)
            .or(verdict_level)
            .map(str::to_owned);
        let source = if encoder.contains_key(id) {
            json!("encoder")
        } else if rule_level.is_some() {
            json!("stated")
        } else if verdict_level.is_some() {
            verdict
                .and_then(|v| v.get("source"))
                .cloned()
                .unwrap_or(Value::Null)
        } else {
            Value::Null
        };
        let mut merged = verdict.cloned().unwrap_or_default();
        merged.insert("level_source".to_owned(), source);
        let wants_beginners = levels
            .iter()
            .any(|level| matches!(*level, "fresher" | "junior" | "intern"));
        let keep_unknown =
            level.is_none() && row.get("min_years").is_none_or(Value::is_null) && wants_beginners;
        if !levels.is_empty()
            && !level.as_deref().is_some_and(|l| levels.contains(&l))
            && !keep_unknown
        {
            continue;
        }
        if !dept_codes.is_empty() && dept_model {
            let code = department_of
                .get(id)
                .map(|(code, _)| code.as_str())
                .unwrap_or("");
            if !dept_codes.contains(code) {
                continue;
            }
        }
        let overlap: Vec<String> = if profile_skills.is_empty() {
            Vec::new()
        } else {
            job_skills
                .iter()
                .filter(|s| profile_skills.contains(*s))
                .cloned()
                .collect()
        };
        if params.match_profile && !profile_skills.is_empty() && overlap.is_empty() {
            continue;
        }
        matches.push(Match {
            row: row.clone(),
            level,
            overlap,
            job_skills,
            verdict: merged,
        });
    }
    let level_rank = |level: Option<&str>| match level.unwrap_or("") {
        "lead" => 0,
        "senior" => 1,
        "mid" => 2,
        "junior" => 3,
        "fresher" => 4,
        "intern" => 5,
        _ => 9,
    };
    let wanted =
        |m: &Match| !levels.is_empty() && m.level.as_deref().is_some_and(|l| levels.contains(&l));
    if params.sort == "level" {
        matches.sort_by_key(|m| std::cmp::Reverse(m.recency()));
        matches.sort_by_key(|m| level_rank(m.level.as_deref()));
    } else if params.sort == "match" && !profile_skills.is_empty() {
        matches.sort_by(|a, b| {
            (wanted(b), b.overlap.len(), b.recency()).cmp(&(
                wanted(a),
                a.overlap.len(),
                a.recency(),
            ))
        });
    } else if !levels.is_empty() {
        let has = |m: &Match| m.level.as_deref().is_some_and(|l| levels.contains(&l));
        matches.sort_by_key(|m| std::cmp::Reverse((has(m), m.recency())));
    }
    // Collapse the same requisition posted in many places (same company, title and skills).
    let mut order: Vec<(String, String, String)> = Vec::new();
    let mut grouped: HashMap<(String, String, String), (usize, Vec<String>)> = HashMap::new();
    let postings_total = matches.len();
    let mut kept: Vec<Match> = Vec::new();
    for item in matches {
        let mut sorted_skills = item.job_skills.clone();
        sorted_skills.sort();
        let key = (
            text(&item.row, "company").to_lowercase(),
            skills::tokens(text(&item.row, "title")).join(" "),
            sorted_skills.join(","),
        );
        match grouped.get_mut(&key) {
            Some((_, extra)) => extra.push(text(&item.row, "location_text").to_owned()),
            None => {
                grouped.insert(key.clone(), (kept.len(), Vec::new()));
                order.push(key);
                kept.push(item);
            }
        }
    }
    let mut extra_locations: HashMap<usize, usize> = HashMap::new();
    for key in &order {
        if let Some((position, extra)) = grouped.get(key) {
            extra_locations.insert(*position, extra.len());
        }
    }
    let size = params.page_size.clamp(1, PAGE_MAX);
    let page = params.page.max(1);
    let total = kept.len() as i64;
    let start = usize::try_from((page - 1) * size).unwrap_or(usize::MAX);
    let mut by_company: Vec<(String, i64)> = Vec::new();
    let mut by_level: Map<String, Value> = Map::new();
    let mut by_department: Vec<(String, i64)> = Vec::new();
    let bump =
        |list: &mut Vec<(String, i64)>, key: &str| match list.iter_mut().find(|(k, _)| k == key) {
            Some(entry) => entry.1 += 1,
            None => list.push((key.to_owned(), 1)),
        };
    for item in &kept {
        bump(&mut by_company, text(&item.row, "company"));
        let level = item.level.clone().unwrap_or_else(|| "unknown".to_owned());
        let count = by_level.get(&level).and_then(Value::as_i64).unwrap_or(0);
        by_level.insert(level, json!(count + 1));
        let code = department_of
            .get(text(&item.row, "id"))
            .map_or("unknown", |(code, _)| code.as_str());
        bump(&mut by_department, code);
    }
    by_company.sort_by_key(|entry| -entry.1);
    by_department.sort_by_key(|entry| -entry.1);
    let jobs: Vec<Value> = kept
        .iter()
        .enumerate()
        .skip(start)
        .take(usize::try_from(size).unwrap_or(15))
        .map(|(position, item)| {
            compact(
                item,
                extra_locations.get(&position).copied().unwrap_or(0),
                department_of.get(text(&item.row, "id")),
            )
        })
        .collect();
    let top_companies: Vec<Value> = by_company
        .into_iter()
        .take(10)
        .map(|(company, count)| json!([company, count]))
        .collect();
    let departments_facet: Map<String, Value> = by_department
        .into_iter()
        .take(12)
        .map(|(code, count)| (code, json!(count)))
        .collect();
    Ok(json!({
        "total_matches": total,
        "total_postings": postings_total,
        "page": page,
        "pages": (total + size - 1) / size,
        "page_size": size,
        "jobs": jobs,
        "facets": {"top_companies": top_companies, "levels": by_level, "departments": departments_facet},
        "filters": {
            "role": params.role, "role_family": family, "levels": levels, "since_hours": params.since_hours,
            "locations": params.locations, "companies": params.companies, "company_slugs": params.company_slugs,
            "title_keywords": params.title_keywords, "remote": params.remote,
            "departments": dept_codes, "departments_not_understood": dept_unknown, "department_model": dept_model,
            "keywords": params.keywords, "match_profile": params.match_profile,
        },
        "classification": {"source": decision_source, "decision_endpoints": [], "pending_decisions": 0, "candidates_scanned": rows.len()},
        "profile_skills_used": profile_skills.len(),
        "index": stats,
    }))
}

fn compact(item: &Match, more_locations: usize, department: Option<&(String, f64)>) -> Value {
    let row = &item.row;
    let date_posted = row.get("date_posted").filter(|v| truthy(v));
    let posted = date_posted.cloned().or_else(|| {
        (number(row, "baseline") == 0.0)
            .then(|| row.get("first_seen_at").cloned())
            .flatten()
            .filter(truthy)
    });
    let basis = if date_posted.is_some() {
        "published"
    } else if posted.is_some() {
        "first_seen"
    } else {
        "unknown"
    };
    let location: String = text(row, "location_text").chars().take(80).collect();
    let url = row
        .get("apply_url")
        .filter(|v| truthy(v))
        .or_else(|| row.get("url"))
        .cloned()
        .unwrap_or(Value::Null);
    json!({
        "job_ref": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "location": location,
        "workplace": row.get("workplace_type"),
        "level": item.level,
        "min_years": row.get("min_years"),
        "posted": posted,
        "posted_basis": basis,
        "matched_skills": item.overlap.iter().take(10).collect::<Vec<_>>(),
        "match_count": item.overlap.len(),
        "top_skills": item.job_skills.iter().take(8).collect::<Vec<_>>(),
        "role_confidence": item.verdict.get("role_p").cloned().unwrap_or(Value::Null),
        "level_source": item.verdict.get("level_source").cloned().unwrap_or(Value::Null),
        "department": department.and_then(|(code, _)| departments::subcategory(code)).map(|(name, _)| name),
        "department_code": department.map(|(code, _)| code),
        "url": url,
        "in_pipeline": row.get("lead_id").is_some_and(truthy),
        "also_posted_in": if more_locations > 0 { json!(more_locations) } else { Value::Null },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_and_terms() {
        assert!(has_word("senior java developer", "java", false));
        assert!(!has_word("javascript developer", "java", false));
        assert!(has_word("data engineer", "data", true));
        assert!(!has_word("teradata engineer", "data", true));
        assert_eq!(
            title_terms(&["Backend".to_owned(), "-Sênior".to_owned()]),
            (vec!["backend".to_owned()], vec!["senior".to_owned()])
        );
        assert_eq!(
            fts_query(&["Go \"lang\"".to_owned(), "c++".to_owned()]),
            "\"go lang\" OR \"c++\""
        );
    }
}
