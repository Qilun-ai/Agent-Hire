//! `agent-hire-api`: the Agent-Hire backend server.
//!
//! The server binds `127.0.0.1`, prints the start-up handshake (`AGENT_HIRE_TOKEN=<token>` unless
//! `--no-token-stdout`, then `PORT:<port>`) and serves the HTTP API.

// Handlers return `Result<_, Response>` so a validation failure can be returned as is; boxing
// every error response would only add noise.
#![allow(clippy::result_large_err)]

mod agent_docs_routes;
mod agent_routes;
mod app_state;
mod apply_routes;
mod auth;
mod cart_routes;
mod cli;
mod data_reset_routes;
mod documents_routes;
mod email_routes;
mod environment_settings;
mod events;
mod events_routes;
mod feedback_relearn;
mod fill_context;
mod fill_job_data;
mod fill_plan;
mod fill_plan_fields;
mod fill_routes;
mod generation_readiness;
mod generation_routes;
mod health;
mod hub;
mod job_index_lifecycle;
mod jobindex_routes;
mod jobindex_runtime_routes;
mod leads_routes;
mod log;
mod middleware;
mod misc_routes;
mod paths;
mod profile_import_routes;
mod profile_routes;
mod profile_source;
mod python_text;
mod rate_limit;
mod respond;
mod resume_ingest;
mod resume_parser;
mod router;
mod settings_routes;
mod telemetry;
mod token;
mod ui_routes;
mod validation;

use std::env;
use std::io;
use std::io::Write;
use std::net::TcpListener;
use std::process::ExitCode;
use std::sync::Arc;

use agent_hire_store::{AppPaths, Database};

use crate::app_state::AppState;
use crate::cli::Options;
use crate::token::Token;

fn main() -> ExitCode {
    let options = match Options::parse(env::args().skip(1)) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("cannot start the async runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    runtime.block_on(run(options))
}

async fn run(options: Options) -> ExitCode {
    // Bind before announcing the port, so nothing can take it between the handshake and serving.
    let listener = match TcpListener::bind(("127.0.0.1", options.port)) {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("cannot bind 127.0.0.1:{}: {error}", options.port);
            return ExitCode::FAILURE;
        }
    };
    let port = match listener.local_addr() {
        Ok(address) => address.port(),
        Err(error) => {
            eprintln!("cannot read the bound port: {error}");
            return ExitCode::FAILURE;
        }
    };
    let token = Token::generate();
    if let Err(error) = write_handshake(&options, &token, port) {
        eprintln!("cannot write the start-up handshake: {error}");
        return ExitCode::FAILURE;
    }

    let app_paths = AppPaths::from_env();
    let database = match Database::open(&app_paths.database()) {
        Ok(database) => database,
        Err(error) => {
            eprintln!("cannot open {}: {error}", app_paths.database().display());
            return ExitCode::FAILURE;
        }
    };
    let repo = paths::RepoPaths::discover();
    let state = Arc::new(AppState::new(token, repo.root, app_paths, database));
    environment_settings::choose_provider_from_environment(&state.settings);
    let mut shutdown = state.shutdown_requested();
    let app = router::build(Arc::clone(&state));

    if let Err(error) = listener.set_nonblocking(true) {
        eprintln!("cannot configure the listener: {error}");
        return ExitCode::FAILURE;
    }
    let listener = match tokio::net::TcpListener::from_std(listener) {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("cannot hand the listener to the runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    job_index_lifecycle::start(Arc::clone(&state)).await;
    log::info("main", "server live");
    let shutdown_state = Arc::clone(&state);
    let served = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            tokio::select! {
                () = termination_signal() => {}
                _ = shutdown.wait_for(|requested| *requested) => {}
            }
            shutdown_state.request_shutdown();
        })
        .await;
    job_index_lifecycle::stop(state).await;
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("server failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn write_handshake(options: &Options, token: &Token, port: u16) -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    if options.print_token {
        writeln!(stdout, "AGENT_HIRE_TOKEN={}", token.as_str())?;
    }
    writeln!(stdout, "PORT:{port}")?;
    stdout.flush()
}

/// Resolves on Ctrl-C, and on Unix also on `SIGTERM`.
async fn termination_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix;

        let Ok(mut terminate) = unix::signal(unix::SignalKind::terminate()) else {
            let _ = tokio::signal::ctrl_c().await;
            return;
        };
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
