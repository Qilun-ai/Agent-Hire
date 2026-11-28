//! Black-box HTTP contract suite for the Agent-Hire backend.
//!
//! The suite only speaks HTTP to a base URL, so the same cases run against the Python backend
//! (`apps/api`, to prove the recorded contract is right) and the Rust backend (to prove parity).
//! Each test group starts its own isolated backend, runs an ordered list of requests through a
//! [`Session`] and compares the normalised transcript with a golden file recorded from Python.
//!
//! Environment:
//! - `CONTRACT_BACKEND`: `rust` (default) or `python` for the archived reference.
//! - `CONTRACT_PYTHON_API_DIR`: archived API directory, required with `CONTRACT_BACKEND=python`.
//! - `CONTRACT_RUST_BIN`: the Rust server binary (default: the workspace's `agent-hire-api`).
//! - `CONTRACT_BASE_URL`: use an already-running backend instead of starting one.
//! - `CONTRACT_RECORD=1`: rewrite the golden files instead of comparing (Python only).
//! - `CONTRACT_MAX_BACKENDS`: how many backends may run at once (default 4).

mod backend;
mod client;
mod golden;
mod page_server;
mod session;

pub use backend::{Backend, Target};
pub use client::{Auth, Body, Client, Response};
pub use golden::normalise;
pub use page_server::{Page, PageServer};
pub use session::Session;
