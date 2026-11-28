/**
 * Application-funnel data derived from REAL pipeline leads.
 *
 * The pipeline tracks each job through: discovered → evaluated → tailoring →
 * approved → applied → interviewing → accepted | rejected (plus discarded).
 * The "Applications" view counts every lead whose status is applied,
 * interviewing, accepted, or rejected — that same set is the funnel here.
 * Status is only the current state; whether an outcome came after an
 * interview is read from each lead's event history.
 *
 * This replaces the original static spec snapshot (73 fake applications); the
 * chart shows nothing until real applications exist.
 *
 * S8A-C6: the funnel is built from COUNTS, not rows — the SQL `/leads/summary`
 * buckets cover every saved application no matter which rows the bounded page
 * holds. A rows-based build remains only as the explicit last-resort fallback,
 * and the view labels it as a partial window rather than the full funnel.
 */

import type { Lead } from "../types";
import type { LeadsSummary } from "../api/leads";

export interface FlowNodeRaw {
  name: string;
  color: string;
  order: number;
}

export interface FlowLinkRaw {
  source: number;
  target: number;
  value: number;
}

/** Stage colors read the app's theme tokens, so dark mode just works. */
const C = {
  hub: "var(--ink)",
  muted: "var(--ink-3)",
  waiting: "var(--yellow)",
  interviewing: "var(--teal)",
  rejected: "var(--bad)",
  offer: "var(--ok)",
};

export const APPLICATION_STATUSES = ["applied", "interviewing", "accepted", "rejected"] as const;

export const applicationsOf = (leads: Lead[]): Lead[] =>
  leads.filter(l => (APPLICATION_STATUSES as readonly string[]).includes(l.status));

/** Funnel stage counts — `awaiting` is the exact 'applied' status, `total` the
 *  whole applied-view bucket, and interviewing/accepted/rejected the current
 *  statuses. The two `*AfterInterview` fields are the subsets of accepted and
 *  rejected whose history shows an interview (always ≤ their parent count). */
export interface ApplicationCounts {
  total: number;
  awaiting: number;
  interviewing: number;
  accepted: number;
  rejected: number;
  acceptedAfterInterview: number;
  rejectedAfterInterview: number;
}

const INTERVIEW_EVENT = "status_changed=interviewing";
const everInterviewed = (lead: Lead) => (lead.events ?? []).some(e => e.action === INTERVIEW_EVENT);

/** Counts from a row list — used only for the complete applications read and
 *  the explicitly-labelled loaded-window fallback. Interview history comes from
 *  `lead.events` when the rows carry it; without it every outcome counts as
 *  "no interview on record". */
export function applicationCounts(leads: Lead[]): ApplicationCounts {
  const apps = applicationsOf(leads);
  const withStatus = (status: string) => apps.filter(l => l.status === status);
  const accepted = withStatus("accepted");
  const rejected = withStatus("rejected");
  return {
    total: apps.length,
    awaiting: withStatus("applied").length,
    interviewing: withStatus("interviewing").length,
    accepted: accepted.length,
    rejected: rejected.length,
    acceptedAfterInterview: accepted.filter(everInterviewed).length,
    rejectedAfterInterview: rejected.filter(everInterviewed).length,
  };
}

/** Counts from the SQL summary — the `applied` bucket is the whole view
 *  (applied+interviewing+accepted+rejected), so 'awaiting reply' is the
 *  remainder after the three outcome buckets. Covers the entire saved set,
 *  independent of which page of rows is loaded. Older servers omit the
 *  after-interview buckets, which then read as 0. */
export function applicationCountsFromSummary(summary: LeadsSummary): ApplicationCounts {
  const counts = summary.counts;
  const interviewing = counts.interviewing ?? 0;
  const accepted = counts.accepted ?? 0;
  const rejected = counts.rejected ?? 0;
  const total = counts.applied ?? 0;
  return {
    total,
    awaiting: Math.max(0, total - interviewing - accepted - rejected),
    interviewing,
    accepted,
    rejected,
    acceptedAfterInterview: Math.min(accepted, counts.accepted_after_interview ?? 0),
    rejectedAfterInterview: Math.min(rejected, counts.rejected_after_interview ?? 0),
  };
}

/** Node names — exported so the view and tests never drift from the chart. */
export const FLOW_NODE = {
  total: "Total Applications",
  awaiting: "Awaiting Reply",
  interviewing: "Interviewing",
  rejected: "Rejected",
  accepted: "Accepted",
  rejectedAfterInterview: "Rejected after interview",
  stillInterviewing: "Still interviewing",
} as const;

/** The two-stage flow the chart draws:
 *
 *    Total ─┬─> Awaiting Reply
 *           ├─> Interviewing ─┬─> Accepted
 *           │                 ├─> Rejected after interview
 *           │                 └─> Still interviewing
 *           └─> Rejected                (no interview on record)
 *
 *  Every accepted lead flows through Interviewing — an offer implies an
 *  interview, even when the status was set by hand and no interview event was
 *  recorded. Rejections split on the interview history. Widths conserve at
 *  every node: Total = awaiting + everInterviewed + rejectedNoInterview, and
 *  Interviewing = accepted + rejectedAfterInterview + stillInterviewing. Stages
 *  with no applications get neither a node nor a link. */
export function buildFlowDataFromCounts(counts: ApplicationCounts): { nodes: FlowNodeRaw[]; links: FlowLinkRaw[] } {
  const rejectedAfter = Math.min(counts.rejected, counts.rejectedAfterInterview);
  const rejectedNoInterview = counts.rejected - rejectedAfter;
  const reachedInterview = counts.interviewing + counts.accepted + rejectedAfter;

  const stages: Array<FlowNodeRaw & { value: number; from?: string }> = [
    { name: FLOW_NODE.total, color: C.hub, order: 0, value: counts.total },
    { name: FLOW_NODE.awaiting, color: C.waiting, order: 1, value: counts.awaiting, from: FLOW_NODE.total },
    { name: FLOW_NODE.interviewing, color: C.interviewing, order: 2, value: reachedInterview, from: FLOW_NODE.total },
    { name: FLOW_NODE.rejected, color: C.rejected, order: 3, value: rejectedNoInterview, from: FLOW_NODE.total },
    { name: FLOW_NODE.accepted, color: C.offer, order: 4, value: counts.accepted, from: FLOW_NODE.interviewing },
    { name: FLOW_NODE.rejectedAfterInterview, color: C.rejected, order: 5, value: rejectedAfter, from: FLOW_NODE.interviewing },
    { name: FLOW_NODE.stillInterviewing, color: C.interviewing, order: 6, value: counts.interviewing, from: FLOW_NODE.interviewing },
  ];
  // Drop empty stages entirely: no zero-width ribbons, no "0" stubs.
  const present = stages.filter(stage => stage.value > 0);
  const nodes: FlowNodeRaw[] = present.map(({ name, color, order }) => ({ name, color, order }));
  const index = new Map(nodes.map((node, i) => [node.name, i]));
  const links: FlowLinkRaw[] = present
    .filter(stage => stage.from && index.has(stage.from))
    .map(stage => ({ source: index.get(stage.from!)!, target: index.get(stage.name)!, value: stage.value }));

  return { nodes, links };
}

/** Rows-based variant kept for the complete applications read and the labelled
 *  loaded-window fallback — identical output to the counts form. */
export function buildFlowData(leads: Lead[]): { nodes: FlowNodeRaw[]; links: FlowLinkRaw[] } {
  return buildFlowDataFromCounts(applicationCounts(leads));
}

const pct = (part: number, whole: number) => (whole === 0 ? 0 : Math.round((part / whole) * 100));

export interface FlowStat {
  label: string;
  value: string;
  sub: string;
  tone: "neutral" | "info" | "good" | "warn" | "bad";
}

export function buildFlowStatsFromCounts(counts: ApplicationCounts): FlowStat[] {
  if (counts.total === 0) return [];
  const heardBack = counts.total - counts.awaiting;

  return [
    { label: "Total Applications", value: String(counts.total), sub: "tracked in your pipeline", tone: "neutral" },
    { label: "Awaiting Reply", value: String(counts.awaiting), sub: `${pct(counts.awaiting, counts.total)}% of applications`, tone: "info" },
    { label: "Response Rate", value: `${pct(heardBack, counts.total)}%`, sub: `${heardBack} of ${counts.total} moved forward`, tone: "info" },
    { label: "In Interviews", value: String(counts.interviewing), sub: `${pct(counts.interviewing, counts.total)}% of applications`, tone: "good" },
    {
      label: "Outcomes",
      value: `${counts.accepted}↑ ${counts.rejected}↓`,
      sub: `${counts.accepted} accepted · ${counts.rejected} rejected`,
      tone: counts.rejected > counts.accepted ? "warn" : "good",
    },
  ];
}

/** Rows-based variant — see buildFlowData. */
export function buildFlowStats(leads: Lead[]): FlowStat[] {
  return buildFlowStatsFromCounts(applicationCounts(leads));
}
