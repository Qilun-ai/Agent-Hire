//! Contract cases, one module per area. Each `#[test]` is one group: a fresh backend, an ordered
//! list of requests, and one golden file in `golden/<group>.json`.
//!
//! Run against Rust: `cargo test -p agent-hire-contract --test contract`.
//! To record against the archived reference, set `CONTRACT_BACKEND=python`,
//! `CONTRACT_PYTHON_API_DIR` and `CONTRACT_RECORD=1` before running the same command.

mod agent_docs;
mod auth;
mod generation;
mod job_search;
mod jobs;
mod lead_feedback;
mod lead_filters;
mod leads;
mod profile;
mod profile_import;
mod settings;
mod tools;
