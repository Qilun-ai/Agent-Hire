// Right-side panel: every company the scraper covers, grouped by how long one scrape takes,
// with live run progress and controls to scrape a whole category or a hand-picked set.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ApiFetch } from "../types";

type Json = Record<string, any>;

const ICON: Record<string, string> = { fast: "⚡", quick: "🟢", medium: "🟡", slow: "🟠", very_slow: "🔴", unknown: "⚪" };

export function duration(seconds?: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(seconds < 36_000 ? 1 : 0)} h`;
}

function ago(iso?: string | null) {
  if (!iso) return "never";
  const m = (Date.now() - new Date(iso).getTime()) / 60_000;
  if (m < 1) return "just now";
  if (m < 60) return `${Math.round(m)}m ago`;
  if (m < 2880) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}

export function CompaniesPanel({ api, open, onClose }: { api: ApiFetch; open: boolean; onClose: () => void }) {
  const [data, setData] = useState<Json | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set(["fast", "quick", "medium", "slow", "very_slow", "unknown", "unavailable"]));
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api("/api/v1/jobindex/companies", { timeoutMs: 20_000 });
      if (r.ok) setData(await r.json());
    } catch { /* keep the last view */ }
  }, [api]);

  useEffect(() => {
    if (!open) return;
    void load();
    const running = Boolean(data?.active_run);
    const t = window.setInterval(load, running ? 5_000 : 30_000);
    const onChange = () => void load();
    window.addEventListener("scrape-changed", onChange);
    return () => { window.clearInterval(t); window.removeEventListener("scrape-changed", onChange); };
  }, [open, load, data?.active_run]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const start = async (body: Json, label: string) => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await api("/api/v1/jobindex/scrape", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const res = await r.json().catch(() => ({}));
      if (!r.ok) setMessage(typeof res.detail === "string" ? res.detail : "Could not start the scrape");
      else if (res.started === false) setMessage(res.reason === "already_running" ? "A scrape is already running — stop it first or wait for it to finish." : `Not started: ${res.reason}`);
      else { setMessage(`Started: ${label} (${res.run?.companies_planned} companies, fastest first).`); setSelected(new Set()); }
      window.dispatchEvent(new CustomEvent("scrape-changed"));
      void load();
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    await api("/api/v1/jobindex/scrape/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    window.dispatchEvent(new CustomEvent("scrape-changed"));
    void load();
  };

  const companies: Json[] = useMemo(() => data?.companies ?? [], [data]);
  const q = query.trim().toLowerCase();
  const visible = q ? companies.filter((c) => c.company.toLowerCase().includes(q) || c.slug.includes(q)) : companies;
  const groups = useMemo(() => {
    const cats: Json[] = data?.categories ?? [];
    const out: Json[] = cats.map((cat) => ({
      ...cat,
      rows: visible.filter((c) => c.status === "verified" && c.category === cat.key).sort((a, b) => (a.est_seconds ?? 1e9) - (b.est_seconds ?? 1e9)),
    }));
    const order = ["no_listing", "blocked", "merged", "verification", "skipped", "review"];
    const unavailable = visible.filter((c) => c.status !== "verified")
      .sort((a, b) => order.indexOf(a.unavailable_group ?? "review") - order.indexOf(b.unavailable_group ?? "review") || a.company.localeCompare(b.company));
    if (unavailable.length) out.push({ key: "unavailable", label: "Not scraped", range: "why each one is unavailable", count: unavailable.length, rows: unavailable, total_seconds: 0, jobs: 0 });
    return out;
  }, [data, visible]);

  const active = data?.active_run;
  const workers = data?.concurrency ?? 4;
  const toggle = (slug: string) => setSelected((prev) => { const n = new Set(prev); if (n.has(slug)) n.delete(slug); else n.add(slug); return n; });
  const toggleGroup = (key: string) => setCollapsed((prev) => { const n = new Set(prev); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const picked = companies.filter((c) => selected.has(c.slug));
  const selectedSeconds = Math.max(
    picked.reduce((sum, c) => sum + (c.est_seconds ?? 0), 0) / Math.max(1, Math.min(workers, picked.length)),
    ...picked.map((c) => c.est_seconds ?? 0), 0,
  );

  return (
    <>
      <div className={`cp-backdrop ${open ? "open" : ""}`} onClick={onClose} />
      <aside className={`cp ${open ? "open" : ""}`} aria-hidden={!open} aria-label="Companies">
        <header className="cp-head">
          <div>
            <h2>Companies</h2>
            <p className="muted">{companies.filter((c) => c.status === "verified").length} scrapeable · grouped by time per company · {workers} scraped in parallel</p>
          </div>
          <button className="cp-close" onClick={onClose} aria-label="Close">×</button>
        </header>

        {active && (
          <div className="cp-run">
            <div className="cp-run-top">
              <span className="tc-live">● Scraping {active.companies_done}/{active.companies_planned} · {active.new_jobs} new jobs</span>
              <button onClick={stop}>Stop</button>
            </div>
            <div className="tc-progress"><div style={{ width: `${Math.round((active.companies_done / Math.max(1, active.companies_planned)) * 100)}%` }} /></div>
          </div>
        )}

        <div className="cp-tools">
          <input placeholder="Search companies" value={query} onChange={(e) => setQuery(e.currentTarget.value)} />
          <button disabled={busy || Boolean(active)} onClick={() => start({}, "all companies")}>Scrape all</button>
        </div>
        {message && <div className="cp-message">{message}</div>}

        <div className="cp-list">
          {!data && <div className="muted cp-empty">Loading…</div>}
          {groups.map((group) => (
            <section key={group.key} className="cp-group">
              <div className="cp-group-head">
                <button className="cp-group-toggle" onClick={() => toggleGroup(group.key)} aria-expanded={!collapsed.has(group.key)}>
                  <span className="cp-caret">{collapsed.has(group.key) ? "▸" : "▾"}</span>
                  <span className="cp-group-icon">{ICON[group.key] ?? "⛔"}</span>
                  <span className="cp-group-title">{group.label}</span>
                  <span className="cp-group-range">{group.range}</span>
                  <span className="cp-group-count">{group.count}</span>
                </button>
                {group.key !== "unavailable" && (
                  <button className="cp-group-scrape" disabled={busy || Boolean(active) || !group.count}
                    title={group.est_wall_seconds != null ? `About ${duration(group.est_wall_seconds)} with ${workers} in parallel (the slowest company takes ${duration(group.longest_seconds)})` : "Not timed yet"}
                    onClick={() => start({ categories: [group.key] }, `${group.label} companies`)}>
                    Scrape{group.est_wall_seconds != null ? ` · ~${duration(group.est_wall_seconds)}` : ""}
                  </button>
                )}
              </div>
              {!collapsed.has(group.key) && (
                <ul className="cp-rows">
                  {group.rows.map((c: Json) => (
                    <li key={c.slug} className={`cp-row ${c.status !== "verified" ? "disabled" : ""}`}>
                      <label>
                        <input type="checkbox" disabled={c.status !== "verified"} checked={selected.has(c.slug)} onChange={() => toggle(c.slug)} />
                        <span className="cp-name" title={c.careers_url}>{c.company}</span>
                      </label>
                      <span className="cp-time" title={c.est_basis === "measured" ? "Measured on your last scrape" : c.est_basis === "recorded" ? "Recorded when the scraper was verified" : ""}>
                        {duration(c.est_seconds)}{c.est_basis === "measured" ? "" : "*"}
                      </span>
                      <span className="cp-jobs">{c.active_jobs ? `${c.active_jobs.toLocaleString()} jobs` : "—"}</span>
                      <span className="cp-state">
                        {c.run_state === "done" ? <span className="cp-badge done">done</span>
                          : c.run_state === "queued" ? <span className="cp-badge queued">queued</span>
                          : c.status === "verified" ? <span className="muted">{ago(c.last_import_at)}</span> : null}
                      </span>
                      {c.status !== "verified" && (
                        <span className="cp-reason" title={c.review_note || ""}>{c.unavailable_reason || "Waiting for review"}</span>
                      )}
                    </li>
                  ))}
                  {!group.rows.length && <li className="muted cp-empty">No companies match.</li>}
                </ul>
              )}
            </section>
          ))}
        </div>

        <footer className="cp-foot">
          <span className="muted">* time recorded when the scraper was verified; replaced by your own measured times after a scrape.</span>
          <button disabled={!selected.size || busy || Boolean(active)} onClick={() => start({ companies: [...selected] }, `${selected.size} selected`)}>
            Scrape {selected.size || ""} selected{selected.size ? ` · ~${duration(selectedSeconds)}` : ""}
          </button>
        </footer>
      </aside>
    </>
  );
}
