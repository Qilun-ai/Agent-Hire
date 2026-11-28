//! The job index: postings scraped from company career sites, searchable by role, level,
//! department, place and freshness.

pub mod autoclassify;
mod datetime;
pub mod decision;
pub mod decision_store;
mod departments;
mod geo;
mod jupyter;
pub mod kaggle;
mod kaggle_code;
mod kaggle_deploy;
mod levels;
mod model_directories;
mod rules;
pub mod runs;
mod search;
mod skills;
mod store;
mod text;

pub use datetime::DateTime;
pub use departments::{
    DEPARTMENT_MIN_CONFIDENCE, DEPARTMENT_ROLE_KEY, LEVEL_MIN_CONFIDENCE, LEVEL_ROLE_KEY, TAXONOMY,
    describe as describe_department, model_available, resolve as resolve_departments,
};
pub use geo::{codes_column, countries, country_name, country_of};
pub use levels::{LevelResult, classify_level, extract_experience, title_level};
pub use model_directories::ModelDirectories;
pub use rules::{LEVELS, normalize_level, parse_posted, role_family, rule_is_software, rule_level};
pub use runs::{
    SPEED_CATEGORIES, SPEED_KEYS, active_run, catalog, company_speeds, import_finished_companies,
    public as public_run, refresh_hours, run_status, scraper_dir, start_run, stop_run,
    verified_slugs,
};
pub use search::{PAGE_MAX, SearchParams, profile_skill_set, run as search};
pub use skills::{extract_skills, normalize_skill, software_skills, tokens};
pub use store::{
    ImportCounts, JobIndex, RULES_VERSION, add_custom_job, decisions_for, delete_jobs, get_job,
    import_company, jobs_dir, lead_id_for, now_iso, posted_expr, recompute_rules, reset_index,
    row_object, sql_json, stats, stats_cached,
};
