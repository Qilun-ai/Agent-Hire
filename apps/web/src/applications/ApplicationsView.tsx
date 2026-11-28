// Applications & chart: the pipeline overview, then every tracked application by stage.
import { useEffect, useMemo, useState } from "react";
import type { LeadsSummary } from "../api/leads";
import { postJson } from "../api/client";
import { AnalyticsView } from "../analytics/AnalyticsView";
import type { ApiFetch, Lead } from "../types";
import { safeUrl } from "../lib/url";

const STAGES: [string, string][] = [
  ["applied", "Applied"],
  ["interviewing", "Interviewing"],
  ["accepted", "Accepted"],
  ["rejected", "Rejected"],
  ["discarded", "Discarded"],
];
const TRACKED = new Set(STAGES.map(([key]) => key));
const PAGE = 100;

function counts(summary: LeadsSummary | null | undefined) {
  const c = summary?.counts ?? {};
  const total = c.applied ?? 0;
  const interviewing = Math.min(c.interviewing ?? 0, total);
  const accepted = Math.min(c.accepted ?? 0, total - interviewing);
  const rejected = Math.min(c.rejected ?? 0, total - interviewing - accepted);
  const discarded = c.discarded ?? 0;
  return { all: total + discarded, applied: total - interviewing - accepted - rejected, interviewing, accepted, rejected, discarded };
}

function updated(lead: Lead): string {
  const value = String(lead.updated_at ?? lead.applied_at ?? lead.created_at ?? "");
  if (!value) return "—";
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? value.slice(0, 10) : date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function ApplicationsView({ api, leads, summary, cartCount }: { api: ApiFetch; leads: Lead[]; summary: LeadsSummary | null; cartCount: number }) {
  const [stage, setStage] = useState("all");
  const [rows, setRows] = useState<Lead[] | null>(null);
  const [limit, setLimit] = useState(PAGE);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const number = counts(summary);

  // `leads` holds every saved job (refreshed live), so filtering here needs no extra request.
  useEffect(() => {
    const tracked = leads.filter((lead) => TRACKED.has(String(lead.status)) && (stage === "all" || lead.status === stage));
    setMore(tracked.length > limit);
    setRows(tracked.slice(0, limit));
  }, [stage, limit, leads]);

  const setStatus = async (lead: Lead, status: string) => {
    setBusy(lead.job_id);
    try {
      const response = await api(`/api/v1/leads/${encodeURIComponent(lead.job_id)}/status`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
      });
      if (!response.ok) throw new Error("Could not change the stage.");
      window.dispatchEvent(new CustomEvent("leads-refresh"));
    } catch (error) {
      setNote({ tone: "bad", text: (error as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const checkEmail = async () => {
    setChecking(true);
    setNote(null);
    try {
      const result = await postJson<{ scanned?: number; updated?: number; error?: string | null }>(api, "/api/v1/email/poll?since_days=14", undefined, 60_000);
      if (result.error) throw new Error(result.error);
      setNote({ tone: "ok", text: `Checked ${result.scanned ?? 0} recent emails · updated ${result.updated ?? 0} application${result.updated === 1 ? "" : "s"}.` });
      window.dispatchEvent(new CustomEvent("leads-refresh"));
    } catch (error) {
      const text = (error as Error).message;
      setNote({ tone: "bad", text: /not configured|not connected/i.test(text) ? "Connect your inbox in Settings → Email updates first." : text });
    } finally {
      setChecking(false);
    }
  };

  const chips = useMemo(() => [["all", "All"], ...STAGES] as [string, string][], []);

  return (
    <div className="applications-page">
      <AnalyticsView api={api} leads={leads} summary={summary} cartCount={cartCount} />
      <div className="apps-bar">
        <div className="apps-chips" role="tablist" aria-label="Filter by stage">
          {chips.map(([key, label]) => (
            <button key={key} role="tab" aria-selected={stage === key} className={`chip-btn ${stage === key ? "active" : ""}`} onClick={() => { setStage(key); setLimit(PAGE); }}>
              {label} ({number[key as keyof typeof number] ?? 0})
            </button>
          ))}
        </div>
        <button className="btn" onClick={() => void checkEmail()} disabled={checking}>{checking ? "Checking…" : "Check email now"}</button>
      </div>
      {note && <div className={`apps-note ${note.tone}`}>{note.text}</div>}
      {rows === null ? (
        <p className="apps-empty">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="apps-empty">No applications here yet. Applications you submit through the extension — and status emails the agent reads — show up here.</p>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table">
            <thead><tr><th>Role</th><th>Company</th><th>Stage</th><th>Updated</th><th className="right">Move to</th></tr></thead>
            <tbody>
              {rows.map((lead) => {
                const href = safeUrl(lead.url);
                return (
                  <tr key={lead.job_id}>
                    <td className="strong">{href ? <a href={href} target="_blank" rel="noreferrer noopener">{lead.title}</a> : lead.title}</td>
                    <td>{lead.company}</td>
                    <td><span className={`stage-pill stage-${lead.status}`}>{STAGES.find(([key]) => key === lead.status)?.[1] ?? lead.status}</span></td>
                    <td className="muted nowrap">{updated(lead)}</td>
                    <td className="right">
                      <select aria-label={`Move ${lead.title} to another stage`} value={String(lead.status)} disabled={busy === lead.job_id} onChange={(event) => void setStatus(lead, (event.target as HTMLSelectElement).value)}>
                        {STAGES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                      </select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {more && <div className="apps-more"><button className="btn" onClick={() => setLimit((value) => value + PAGE)}>Load more</button></div>}
    </div>
  );
}
