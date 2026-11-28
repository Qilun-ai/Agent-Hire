// Shared types for the Agent-Hire web interface.

/** Options accepted by {@link ApiFetch} on top of the standard fetch options. */
export interface ApiRequestInit extends RequestInit {
  /** Abort the request after this many milliseconds (default 30 s). */
  timeoutMs?: number;
}

/** A fetch bound to the local Agent-Hire API that adds the bearer token. */
export type ApiFetch = (path: string, init?: ApiRequestInit) => Promise<Response>;

export type SettingsTab = "ai" | "email" | "agent" | "general";

export type LeadStatus = "discovered" | "approved" | "applied" | "interviewing" | "accepted" | "rejected" | "discarded" | string;

export interface LeadEvent {
  at?: string;
  ts?: string;
  status?: string;
  kind?: string;
  note?: string;
  [key: string]: unknown;
}

/** A job the user has kept (shortlisted, applied to, …), as returned by /api/v1/leads. */
export interface Lead {
  job_id: string;
  title: string;
  company: string;
  url?: string;
  location?: string;
  status: LeadStatus;
  platform?: string;
  created_at?: string;
  updated_at?: string;
  applied_at?: string;
  resume_asset?: string;
  asset?: string;
  events?: LeadEvent[];
  [key: string]: unknown;
}
