//! Reading job text: the seniority band a posting is for.

mod seniority;

pub use seniority::{Seniority, classify_text, job_seniority, resolve_lead_seniority};
