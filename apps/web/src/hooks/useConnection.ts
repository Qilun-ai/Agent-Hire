// Connects the page to the local API: fetches the token, then keeps a WebSocket open to /ws for
// live events. Every non-heartbeat event is re-broadcast as a window "api-event", so views can
// refresh without each opening its own socket.
import { useEffect, useMemo, useState } from "react";
import { createApiFetch, loadBootstrap } from "../api/client";
import type { ApiFetch } from "../types";

export type ConnectionState = "connecting" | "online" | "offline";

export interface Connection {
  state: ConnectionState;
  token: string | null;
  version: string;
  api: ApiFetch | null;
  error: string | null;
}

const SUBPROTOCOL = "agent-hire.bearer";

export function useConnection(): Connection {
  const [token, setToken] = useState<string | null>(null);
  const [version, setVersion] = useState("");
  const [state, setState] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);

  // The token: retried until the API answers (it may still be starting).
  useEffect(() => {
    let stopped = false;
    let delay = 500;
    const attempt = () => {
      loadBootstrap()
        .then((boot) => {
          if (stopped) return;
          setToken(boot.token);
          setVersion(boot.version);
          setError(null);
        })
        .catch((reason: Error) => {
          if (stopped) return;
          setError(reason.message);
          setState("offline");
          setTimeout(attempt, delay);
          delay = Math.min(delay * 2, 8000);
        });
    };
    attempt();
    return () => {
      stopped = true;
    };
  }, []);

  // The live socket: reconnects with backoff; a lost socket marks the app offline.
  useEffect(() => {
    if (!token) return;
    let socket: WebSocket | null = null;
    let stopped = false;
    let delay = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const open = () => {
      if (stopped) return;
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${scheme}//${location.host}/ws`, [SUBPROTOCOL, token]);
      socket.onopen = () => {
        delay = 1000;
        setState("online");
      };
      socket.onmessage = (message) => {
        let event: { type?: string } & Record<string, unknown>;
        try {
          event = JSON.parse(String(message.data));
        } catch {
          return;
        }
        if (event.type === "heartbeat" || event.type === "pong") return;
        window.dispatchEvent(new CustomEvent("api-event", { detail: event }));
      };
      socket.onclose = () => {
        if (stopped) return;
        setState("offline");
        timer = setTimeout(open, delay);
        delay = Math.min(delay * 2, 15_000);
      };
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [token]);

  const api = useMemo(() => (token === null ? null : createApiFetch(token)), [token]);
  return { state: token === null ? state : state === "connecting" ? "online" : state, token, version, api, error };
}
