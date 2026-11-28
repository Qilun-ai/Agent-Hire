// Client for the Agent-Hire agent service (/agent/*), served same-origin by the agent front door.

export interface SessionSummary {
  id: string;
  title: string;
  updated_at: string;
  messages: number;
  running: boolean;
}

export interface DisplayItem {
  kind: "user" | "assistant" | "tool";
  text?: string;
  source?: "user" | "schedule";
  id?: string;
  name?: string;
  args?: unknown;
  result?: string;
  details?: unknown;
  isError?: boolean;
  timestamp?: number;
  error?: string;
  /** client-only: still streaming */
  pending?: boolean;
}

export interface Transcript {
  id: string;
  title: string;
  seq: number;
  running: boolean;
  items: DisplayItem[];
}

export interface ChatEvent {
  seq?: number;
  type: string;
  [key: string]: unknown;
}

export interface Schedule {
  id: string;
  sessionId: string;
  instruction: string;
  nextRunAt: string;
  repeatEveryMinutes?: number;
  runs: number;
}

export class AgentApi {
  constructor(private readonly token: string) {}

  private url(path: string): string {
    return path;
  }

  async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(this.url(path), {
      method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!(res.headers.get("content-type") || "").includes("application/json")) {
      // An older Agent-Hire backend on this port answers /agent/* with the web page instead of JSON.
      throw new Error("The Agent-Hire agent service isn't answering on this port. Stop the old app and start this version with `make up`.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data as { detail?: string }).detail || `Request failed (${res.status})`);
    return data as T;
  }

  status() {
    return this.call<{ llm: { configured: boolean; provider?: string; model?: string; reason?: string }; schedules: number }>("GET", "/agent/status");
  }
  sessions() {
    return this.call<{ sessions: SessionSummary[] }>("GET", "/agent/sessions").then((r) => r.sessions);
  }
  createSession() {
    return this.call<{ id: string }>("POST", "/agent/sessions").then((r) => r.id);
  }
  deleteSession(id: string) {
    return this.call<{ deleted: boolean }>("DELETE", `/agent/sessions/${id}`);
  }
  transcript(id: string) {
    return this.call<Transcript>("GET", `/agent/sessions/${id}`);
  }
  send(id: string, text: string, attachments: string[] = []) {
    return this.call<{ accepted: boolean }>("POST", `/agent/sessions/${id}/messages`, { text, attachments });
  }
  abort(id: string) {
    return this.call<{ aborted: boolean }>("POST", `/agent/sessions/${id}/abort`, {});
  }
  schedules(sessionId?: string) {
    const q = sessionId ? `?session=${encodeURIComponent(sessionId)}` : "";
    return this.call<{ schedules: Schedule[] }>("GET", `/agent/schedules${q}`).then((r) => r.schedules);
  }
  cancelSchedule(id: string) {
    return this.call<{ removed: boolean }>("DELETE", `/agent/schedules/${id}`);
  }

  async upload(file: File): Promise<{ upload_id: string; name: string; size: number }> {
    const form = new FormData();
    form.set("file", file);
    const res = await fetch(this.url("/agent/uploads"), { method: "POST", headers: { Authorization: `Bearer ${this.token}` }, body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data as { detail?: string }).detail || `Upload failed (${res.status})`);
    return data as { upload_id: string; name: string; size: number };
  }

  /** Session event stream from a cursor. Reconnects with backoff and resumes after the last seen seq. */
  streamSession(id: string, after: number, onEvent: (event: ChatEvent) => void): () => void {
    let cursor = after;
    let source: EventSource | null = null;
    let closed = false;
    let delay = 500;
    const open = () => {
      if (closed) return;
      source = new EventSource(this.url(`/agent/sessions/${id}/events?after=${cursor}&token=${encodeURIComponent(this.token)}`));
      source.onmessage = (message) => {
        delay = 500;
        const event = JSON.parse(message.data) as ChatEvent;
        if (typeof event.seq === "number") {
          if (event.seq <= cursor) return; // duplicate after reconnect
          cursor = event.seq;
        }
        onEvent(event);
      };
      source.onerror = () => {
        source?.close();
        if (closed) return;
        setTimeout(open, delay);
        delay = Math.min(delay * 2, 10_000);
      };
    };
    open();
    return () => {
      closed = true;
      source?.close();
    };
  }

  streamHost(onEvent: (event: ChatEvent) => void): () => void {
    let source: EventSource | null = null;
    let closed = false;
    const open = () => {
      if (closed) return;
      source = new EventSource(this.url(`/agent/events?token=${encodeURIComponent(this.token)}`));
      source.onmessage = (message) => onEvent(JSON.parse(message.data));
      source.onerror = () => {
        source?.close();
        if (!closed) setTimeout(open, 3000);
      };
    };
    open();
    return () => {
      closed = true;
      source?.close();
    };
  }
}
