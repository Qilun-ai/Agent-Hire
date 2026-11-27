//! The `/ws` live-event websocket.
//!
//! Browsers authenticate with the subprotocols `["agent-hire.bearer", <token>]`; other clients may send
//! `Authorization: Bearer <token>`. The application hub fans out events to each client.

use std::sync::Arc;
use std::time::Duration;

use agent_hire_store::UtcTime;

use axum::extract::State;
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use futures_util::SinkExt;
use serde_json::json;
use tokio::time::{self, Instant, MissedTickBehavior};

use crate::app_state::AppState;
use crate::log;

const SUBPROTOCOL: &str = "agent-hire.bearer";
const HEARTBEAT: Duration = Duration::from_secs(15);
const SEND_TIMEOUT: Duration = Duration::from_secs(2);

/// Upgrades an authenticated request; anything else gets `403` before the handshake.
pub async fn websocket(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    upgrade: WebSocketUpgrade,
) -> Response {
    let from_protocol = subprotocol_token(&headers);
    let from_header = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    if !from_protocol
        .into_iter()
        .chain(from_header)
        .any(|token| state.token.matches(token))
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    let upgrade = if offered_bearer_protocol(&headers) {
        upgrade.protocols([SUBPROTOCOL])
    } else {
        upgrade
    };
    upgrade.on_upgrade(move |socket| serve(socket, state))
}

fn offered_bearer_protocol(headers: &HeaderMap) -> bool {
    headers
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .is_some_and(|first| first.trim() == SUBPROTOCOL)
}

/// The token from `Sec-WebSocket-Protocol: agent-hire.bearer, <token>`.
fn subprotocol_token(headers: &HeaderMap) -> Option<&str> {
    let offered = headers.get(header::SEC_WEBSOCKET_PROTOCOL)?.to_str().ok()?;
    let mut parts = offered.split(',').map(str::trim);
    (parts.next()? == SUBPROTOCOL)
        .then(|| parts.next())
        .flatten()
}

async fn serve(mut socket: WebSocket, state: Arc<AppState>) {
    let Ok(_slot) = state.websocket_slots.try_acquire() else {
        let _ = send(
            &mut socket,
            Message::Close(Some(CloseFrame {
                code: 1013,
                reason: "too many websocket connections".into(),
            })),
        )
        .await;
        return;
    };
    let mut local = state.hub.subscribe();
    let mut shutdown = state.shutdown_requested();
    let mut interval = time::interval_at(Instant::now() + HEARTBEAT, HEARTBEAT);
    interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut beat = 0;
    loop {
        let message = tokio::select! {
            incoming = socket.recv() => match incoming {
                Some(Ok(Message::Text(text))) if text == "ping" => Message::Text(r#"{"type":"pong"}"#.into()),
                None | Some(Ok(Message::Close(_))) => break,
                Some(Err(error)) => {
                    log::warning("ws", &error.to_string());
                    break;
                }
                _ => continue,
            },
            incoming = local.recv() => match incoming {
                Ok(text) => Message::Text(text.into()),
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            },
            _ = interval.tick() => {
                beat += 1;
                let uptime = (state.started.elapsed().as_secs_f64() * 100.0).round_ties_even() / 100.0;
                Message::Text(json!({
                    "type":"heartbeat","status":"alive","beat":beat,
                    "uptime_seconds":uptime,"timestamp":UtcTime::now().to_iso_micros(),
                }).to_string().into())
            }
            _ = shutdown.wait_for(|requested| *requested) => break,
        };
        if !send(&mut socket, message).await {
            return;
        }
    }
    let _ = time::timeout(SEND_TIMEOUT, socket.close()).await;
}

async fn send(socket: &mut WebSocket, message: Message) -> bool {
    matches!(
        time::timeout(SEND_TIMEOUT, socket.send(message)).await,
        Ok(Ok(()))
    )
}
