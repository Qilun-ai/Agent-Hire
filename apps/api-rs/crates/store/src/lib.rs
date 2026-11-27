//! SQLite storage for the Agent-Hire backend.
//!
//! The app database (`crm.db`) is opened in place, so existing installs keep their data. A fresh
//! database gets the schema the Python backend's 18 migrations produce (`schema/crm.sql`, dumped
//! from a fresh install), with the same `schema_migrations` rows, so either backend can open it.

mod app_paths;
pub mod clock;
mod data_reset;
mod database;
mod documents;
mod events;
mod generation_records;
mod lead_cleanup;
mod lead_feedback;
mod lead_filters;
mod lead_identity;
mod leads;
mod profile;
mod profile_deletions;
mod profile_entries;
mod profile_import;
mod profile_normalization;
mod profile_points;
mod profile_projects;
mod profile_skills;
mod profile_text;
mod py_json;
mod settings;

pub use app_paths::AppPaths;
pub use clock::UtcTime;
pub use data_reset::reset as reset_data;
pub use database::{Database, StoreError};
pub use documents::{DocumentStore, DocumentUpload};
pub use events::{Event, EventLog, truncate_chars};
pub use generation_records::{GenerationJobUpdate, GenerationScope};
pub use lead_feedback::LearningInputs;
pub use lead_filters::{
    LeadFilters, LeadSort, LeadView, SeniorityFilter, normalize_location, normalize_skill,
};
pub use leads::{LeadError, LeadStore, annotate_seniority, is_valid_status};
pub use profile::{ProfileEntryKind, ProfileStore};
pub use py_json::dumps as python_json_dumps;
pub use settings::{SettingsError, SettingsStore};
