//! Email monitoring: connect the address you apply from; the app reads it (never changes it),
//! works out which application each message is about and what happened, and moves the pipeline.

mod classify;
mod client;
mod decision;

pub use classify::{
    LeadMatch, Outcome, OutcomeVerdict, classify_outcome, match_lead, outcome_status,
};
pub use client::{DEFAULT_PORT, EmailError, MailboxConfig, Message, check, recent};
pub use decision::{
    DECISION_SYSTEM, company_list, decision_prompt, decision_schema, is_candidate,
    verdict_from_decision,
};
