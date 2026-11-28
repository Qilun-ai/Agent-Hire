// Same-origin client for the local Agent-Hire API. The page is served by the app itself, so the API
// is on this origin; /bootstrap hands the page its bearer token (the server only listens on loopback).
import type { ApiFetch, ApiRequestInit } from "../types";

export interface Bootstrap {
  token: string;
  version: string;
}

let bootstrapPromise: Promise<Bootstrap> | null = null;

/** Fetches the token once per page load; retries on the next call if the API was not up yet. */
export function loadBootstrap(): Promise<Bootstrap> {
  bootstrapPromise ??= fetch("/bootstrap", { cache: "no-store", credentials: "same-origin" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`the API answered ${response.status}`);
      const body = (await response.json()) as Partial<Bootstrap>;
      return { token: body.token ?? "", version: body.version ?? "" };
    })
    .catch((error: unknown) => {
      bootstrapPromise = null;
      throw error instanceof Error ? error : new Error(String(error));
    });
  return bootstrapPromise;
}

export function createApiFetch(token: string): ApiFetch {
  return async (path: string, init: ApiRequestInit = {}) => {
    const { timeoutMs = 30_000, headers, signal, ...rest } = init;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new DOMException("The request timed out", "TimeoutError")), timeoutMs);
    signal?.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
    try {
      return await fetch(path, {
        ...rest,
        headers: { Authorization: `Bearer ${token}`, ...(headers as Record<string, string> | undefined) },
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  };
}

/** POSTs JSON and returns the parsed reply, throwing the server's `detail` on failure. */
export async function postJson<T>(api: ApiFetch, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  const response = await api(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    timeoutMs,
  });
  const data = (await response.json().catch(() => ({}))) as T & { detail?: unknown };
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
  return data;
}

export async function getJson<T>(api: ApiFetch, path: string): Promise<T> {
  const response = await api(path);
  const data = (await response.json().catch(() => ({}))) as T & { detail?: unknown };
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
  return data;
}
