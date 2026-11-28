// Tool-call cards in the chat, dispatched by tool name with a generic fallback.
import { useState } from "react";
import type { DisplayItem } from "./agentApi";
import { safeUrl } from "../lib/url";

type Json = Record<string, any>;

const LABELS: Record<string, string> = {
  get_profile: "Read your profile",
  import_document: "Saved & read document",
  read_document: "Read document",
  save_profile: "Saved profile",
  set_preferences: "Saved preferences",
  job_index_status: "Checked the job index",
  start_scrape: "Started scraping",
  scrape_status: "Scrape progress",
  search_jobs: "Searched jobs",
  get_job: "Read job details",
  shortlist_jobs: "Shortlisted jobs",
  tailor_resume: "Tailored résumé",
  extension_queue: "Extension queue",
  application_summary: "Application tracker",
  check_email: "Checked email",
  schedule_check: "Scheduled a check-in",
  classify_jobs: "Classified job levels",
  add_custom_job: "Added a job",
  delete_jobs: "Deleted jobs",
  update_job_status: "Updated application stage",
  list_pipeline: "Listed applications",
  reset_jobs: "Reset job data",
  edit_profile: "Edited profile",
  list_schedules: "Scheduled check-ins",
  cancel_schedule: "Cancelled a check-in",
};

function when(iso?: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hours = (Date.now() - d.getTime()) / 3_600_000;
  if (hours < 0) return d.toLocaleString();
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m ago`;
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return d.toLocaleDateString();
}

function JobsTable({ jobs }: { jobs: Json[] }) {
  if (!jobs?.length) return null;
  return (
    <div className="tc-table-wrap">
      <table className="tc-table">
        <thead><tr><th>Role</th><th>Company</th><th>Location</th><th>Level</th><th>Posted</th><th>Skills matched</th></tr></thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_ref}>
              <td>{safeUrl(job.url) ? <a href={safeUrl(job.url)} target="_blank" rel="noreferrer noopener">{job.title}</a> : job.title}</td>
              <td>{job.company}</td>
              <td className="muted">{job.location || "—"}</td>
              <td>{job.level || "—"}</td>
              <td className="muted" title={job.posted_basis === "first_seen" ? "First seen by Agent-Hire" : "Published by the company"}>
                {when(job.posted)}{job.posted_basis === "first_seen" ? "*" : ""}
              </td>
              <td className="muted">{(job.matched_skills || []).slice(0, 4).join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Body({ item }: { item: DisplayItem }) {
  const d = (item.details ?? {}) as Json;
  switch (item.name) {
    case "search_jobs":
      return (
        <>
          <div className="tc-stats">
            <span><b>{d.total_matches ?? 0}</b> matches</span>
            {d.pages > 1 && <span>page {d.page}/{d.pages}</span>}
            {d.classification && <span>classified by {d.classification.source === "rules" ? "rules" : `decision model (${d.classification.source})`}</span>}
            {d.classification?.pending_decisions > 0 && <span>{d.classification.pending_decisions} awaiting the decision model</span>}
          </div>
          <JobsTable jobs={d.jobs} />
        </>
      );
    case "job_index_status": {
      const idx = d.index ?? {};
      const active = d.scrape?.active;
      return (
        <div className="tc-stats">
          <span><b>{idx.jobs_active ?? 0}</b> open jobs</span>
          <span><b>{idx.jobs_last_24h ?? 0}</b> from the last 24h</span>
          <span>{idx.companies_indexed ?? 0}/{d.companies_available ?? "?"} companies indexed</span>
          {active && <span className="tc-live">scraping {active.companies_done}/{active.companies_planned}</span>}
        </div>
      );
    }
    case "start_scrape":
    case "scrape_status": {
      const run = d.run ?? d.active ?? (d.recent ?? [])[0];
      if (!run) return <div className="tc-stats"><span>{d.reason ? `Not started: ${String(d.reason).replace(/_/g, " ")}` : "No scrape has run yet"}</span></div>;
      const pct = run.companies_planned ? Math.round((run.companies_done / run.companies_planned) * 100) : 0;
      return (
        <div>
          <div className="tc-stats">
            <span className={run.status === "running" ? "tc-live" : ""}>{run.status}</span>
            <span>{run.companies_done}/{run.companies_planned} companies</span>
            <span><b>{run.new_jobs}</b> new jobs</span>
            <span>started {when(run.started_at)}</span>
          </div>
          <div className="tc-progress"><div style={{ width: `${pct}%` }} /></div>
        </div>
      );
    }
    case "import_document":
    case "read_document":
      return (
        <div className="tc-stats">
          <span>{d.file}</span>
          {d.pages ? <span>{d.pages} page{d.pages > 1 ? "s" : ""}</span> : null}
          <span>{(d.total_chars ?? 0).toLocaleString()} characters</span>
          {d.note && <span className="tc-live">scanned — read as images</span>}
        </div>
      );
    case "edit_profile":
    case "save_profile":
    case "get_profile": {
      const p = d.profile ?? {};
      return (
        <div className="tc-profile">
          {p.name && <div className="tc-profile-name">{p.name}</div>}
          {p.skills_count !== undefined && <div className="muted">{p.skills_count} skills · {(p.experience ?? []).length} roles · {(p.projects ?? []).length} projects</div>}
          {p.skills?.length > 0 && <div className="tc-chips">{p.skills.slice(0, 18).map((s: string) => <span key={s} className="tc-chip">{s}</span>)}</div>}
        </div>
      );
    }
    case "shortlist_jobs":
      return (
        <div className="tc-stats">
          <span><b>{(d.added ?? []).length}</b> added to Applications &amp; the extension queue</span>
          {(d.already_in_pipeline ?? []).length > 0 && <span>{d.already_in_pipeline.length} already there</span>}
        </div>
      );
    case "tailor_resume": {
      const lead = d.lead ?? {};
      return (
        <div className="tc-stats">
          <span>{lead.title} — {lead.company}</span>
          <span className={d.status === "ready" ? "tc-ok" : ""}>{d.status === "ready" ? "résumé ready" : d.status}</span>
        </div>
      );
    }
    case "schedule_check": {
      const s = d.id ? d : d.scheduled ?? {};
      return (
        <div className="tc-stats">
          <span>next {new Date(s.nextRunAt).toLocaleString()}</span>
          {s.repeatEveryMinutes && <span>every {s.repeatEveryMinutes >= 60 ? `${Math.round(s.repeatEveryMinutes / 60)}h` : `${s.repeatEveryMinutes}m`}</span>}
          <span className="muted">“{String(s.instruction ?? "").slice(0, 120)}”</span>
        </div>
      );
    }
    default:
      return null;
  }
}

export function ToolCard({ item }: { item: DisplayItem }) {
  const [open, setOpen] = useState(false);
  const label = LABELS[item.name ?? ""] ?? item.name;
  const running = item.result === undefined && !item.isError;
  return (
    <div className={`tc ${item.isError ? "tc-error" : ""}`}>
      <button className="tc-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`tc-dot ${running ? "tc-dot-run" : item.isError ? "tc-dot-err" : "tc-dot-ok"}`} />
        <span className="tc-label">{label}{running ? "…" : ""}</span>
        <span className="tc-toggle">{open ? "hide" : "details"}</span>
      </button>
      {!running && !item.isError && <div className="tc-body"><Body item={item} /></div>}
      {item.isError && <div className="tc-body tc-err-text">{item.result}</div>}
      {open && (
        <div className="tc-raw">
          <div className="tc-raw-label">input</div>
          <pre>{JSON.stringify(item.args ?? {}, null, 2)}</pre>
          {item.result !== undefined && (<><div className="tc-raw-label">result</div><pre>{item.result}</pre></>)}
        </div>
      )}
    </div>
  );
}
