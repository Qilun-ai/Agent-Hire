// Jobs tab: browse the scraped job index, manage scraping, and see what the agent shortlisted.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiFetch, Lead } from "../types";
import { safeUrl } from "../lib/url";

type Json = Record<string, any>;

const WINDOWS: { label: string; hours: number | null }[] = [
  { label: "Any time", hours: null },
  { label: "Last 24 hours", hours: 24 },
  { label: "Last 3 days", hours: 72 },
  { label: "Last week", hours: 168 },
  { label: "Last month", hours: 720 },
];
const LEVELS = ["intern", "fresher", "junior", "mid", "senior", "lead"];

function ago(iso?: string | null) {
  if (!iso) return "—";
  const h = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (Number.isNaN(h)) return "—";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function JobsView({ api, leads }: { api: ApiFetch; leads: Lead[] }) {
  const [sub, setSub] = useState<"index" | "shortlist">("index");
  const [status, setStatus] = useState<Json | null>(null);
  const [role, setRole] = useState("software engineer");
  const [levels, setLevels] = useState<string[]>([]);
  const [hours, setHours] = useState<number | null>(null);
  const [location, setLocation] = useState("");
  const [keywords, setKeywords] = useState("");
  const [titleWords, setTitleWords] = useState("");
  const [companies, setCompanies] = useState<string[]>([]);
  const [departments, setDepartments] = useState<string[]>([]);
  const [matchProfile, setMatchProfile] = useState(false);
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<Json | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    const r = await api("/api/v1/jobindex/status");
    if (r.ok) setStatus(await r.json());
  }, [api]);

  useEffect(() => {
    void loadStatus();
    const t = window.setInterval(loadStatus, 20_000);
    return () => window.clearInterval(t);
  }, [loadStatus]);

  const search = useCallback(async (nextPage = 1) => {
    setBusy(true);
    setMessage(null);
    try {
      const body = {
        role, levels, since_hours: hours, locations: location ? location.split(",").map((s) => s.trim()).filter(Boolean) : [],
        keywords: keywords ? keywords.split(",").map((s) => s.trim()).filter(Boolean) : [],
        title_keywords: titleWords ? titleWords.split(",").map((s) => s.trim()).filter(Boolean) : [],
        company_slugs: companies, departments,
        match_profile: matchProfile, sort: matchProfile ? "match" : "recent", page: nextPage, page_size: 25,
        // Use the verdicts already saved and answer at once: classifying hundreds of jobs inline on a Kaggle
        // notebook takes many minutes; the background classifier keeps filling them in.
        use_decision_model: false,
      };
      const r = await api("/api/v1/jobindex/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs: 600_000 });
      const data = await r.json();
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Search failed");
      setResult(data);
      setPage(nextPage);
      setSelected(new Set());
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [api, role, levels, hours, location, keywords, titleWords, companies, departments, matchProfile]);

  useEffect(() => { void search(1); /* initial load */ // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shortlist = async () => {
    if (!selected.size) return;
    const r = await api("/api/v1/jobindex/promote", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_refs: [...selected], add_to_cart: true }) });
    const data = await r.json();
    setMessage(r.ok ? `${data.added.length} added to Applications and the extension queue${data.already_in_pipeline.length ? ` (${data.already_in_pipeline.length} were already there)` : ""}.` : "Could not shortlist");
    window.dispatchEvent(new CustomEvent("leads-refresh"));
    void search(page);
  };

  const scrape = async (stop = false) => {
    const r = await api(stop ? "/api/v1/jobindex/scrape/stop" : "/api/v1/jobindex/scrape", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await r.json();
    if (!stop && data.started === false) setMessage(data.reason === "already_running" ? "A scrape is already running." : `Not started: ${data.reason}`);
    window.dispatchEvent(new CustomEvent("scrape-changed"));
    void loadStatus();
  };

  const idx = status?.index ?? {};
  const active = status?.scrape?.active;
  const last = (status?.scrape?.recent ?? []).find((r: Json) => r.status !== "running");
  const shortlisted = leads.filter((l) => ["discovered", "evaluating", "tailoring", "approved"].includes(l.status));

  return (
    <div className="jobs">
      <div className="jobs-tabs">
        <button className={sub === "index" ? "active" : ""} onClick={() => setSub("index")}>All scraped jobs</button>
        <button className={sub === "shortlist" ? "active" : ""} onClick={() => setSub("shortlist")}>Shortlisted ({shortlisted.length})</button>
      </div>

      <div className="jobs-status">
        <div><b>{(idx.jobs_active ?? 0).toLocaleString()}</b><span>open jobs</span></div>
        <div><b>{(idx.jobs_last_24h ?? 0).toLocaleString()}</b><span>last 24h</span></div>
        <div><b>{idx.companies_indexed ?? 0}/{status?.companies_available ?? "—"}</b><span>companies</span></div>
        <div className="jobs-scrape">
          {active ? (
            <>
              <span className="tc-live">Scraping {active.companies_done}/{active.companies_planned} · {active.new_jobs} new</span>
              <div className="tc-progress"><div style={{ width: `${Math.round((active.companies_done / Math.max(1, active.companies_planned)) * 100)}%` }} /></div>
              <button onClick={() => scrape(true)}>Stop</button>
            </>
          ) : (
            <>
              <span className="muted">{last ? `Last scrape ${ago(last.finished_at || last.started_at)} · refresh every ${status?.scrape?.refresh_hours ?? 12}h` : "Not scraped yet"}</span>
              <button onClick={() => scrape(false)}>Scrape all companies</button>
            </>
          )}
        </div>
      </div>

      {sub === "index" ? (
        <>
          <form className="jobs-filters" onSubmit={(e) => { e.preventDefault(); void search(1); }}>
            <input value={role} onChange={(e) => setRole(e.currentTarget.value)} placeholder="Role (e.g. software engineer)" />
            <select value={hours ?? ""} onChange={(e) => setHours(e.currentTarget.value ? Number(e.currentTarget.value) : null)}>
              {WINDOWS.map((w) => <option key={w.label} value={w.hours ?? ""}>{w.label}</option>)}
            </select>
            <input value={location} onChange={(e) => setLocation(e.currentTarget.value)} placeholder="Locations (comma separated)" />
            <input value={keywords} onChange={(e) => setKeywords(e.currentTarget.value)} placeholder="Skills / keywords" />
            <input value={titleWords} onChange={(e) => setTitleWords(e.currentTarget.value)} placeholder="Title has (e.g. backend, -senior)"
              title="Words the job title must contain (any of them). Put - in front to exclude: backend, python, -senior, -manager" />
            <CompanyPicker api={api} value={companies} onChange={setCompanies} />
            <DepartmentPicker api={api} value={departments} onChange={setDepartments} />
            <div className="jobs-levels">
              {LEVELS.map((l) => (
                <label key={l} className={levels.includes(l) ? "on" : ""}>
                  <input type="checkbox" checked={levels.includes(l)} onChange={() => setLevels((prev) => prev.includes(l) ? prev.filter((x) => x !== l) : [...prev, l])} />{l}
                </label>
              ))}
            </div>
            <label className="jobs-match"><input type="checkbox" checked={matchProfile} onChange={(e) => setMatchProfile(e.currentTarget.checked)} /> Match my skills</label>
            <button type="submit" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
          </form>

          {message && <div className="jobs-message">{message}</div>}

          <div className="jobs-results-head">
            <span>{result ? `${result.total_matches.toLocaleString()} jobs` : ""}{result?.classification?.source && result.classification.source !== "rules" ? ` · classified by ${result.classification.source}` : ""}</span>
            <button disabled={!selected.size} onClick={shortlist}>Add {selected.size || ""} to applications</button>
          </div>

          <div className="jobs-table-wrap">
            <table className="jobs-table">
              <thead>
                <tr><th /><th>Role</th><th>Company</th><th>Department</th><th>Location</th><th>Level</th><th>Posted</th><th>{matchProfile ? "Your skills it wants" : "Skills"}</th></tr>
              </thead>
              <tbody>
                {(result?.jobs ?? []).map((j: Json) => (
                  <tr key={j.job_ref} className={j.in_pipeline ? "in-pipeline" : ""}>
                    <td><input type="checkbox" disabled={j.in_pipeline} checked={selected.has(j.job_ref)} onChange={() => setSelected((prev) => { const n = new Set(prev); if (n.has(j.job_ref)) n.delete(j.job_ref); else n.add(j.job_ref); return n; })} /></td>
                    <td>{safeUrl(j.url) ? <a href={safeUrl(j.url)} target="_blank" rel="noreferrer noopener">{j.title}</a> : j.title}{j.in_pipeline && <span className="pill">shortlisted</span>}</td>
                    <td>{j.company}</td>
                    <td className="muted" title={j.department_code ? `${j.department_code}` : "Not classified yet, or the model was unsure"}>{j.department || "—"}</td>
                    <td className="muted">{j.location || "—"}{j.also_posted_in ? <span title="The same opening is also posted in other locations"> · +{j.also_posted_in} more</span> : null}</td>
                    <td>{j.level || "—"}{j.min_years != null ? <span className="muted"> · {j.min_years}+y</span> : null}</td>
                    <td className="muted" title={j.posted_basis === "first_seen" ? "When Agent-Hire first saw it" : j.posted_basis === "published" ? "Published by the company" : "Unknown"}>{ago(j.posted)}{j.posted_basis === "first_seen" ? "*" : ""}</td>
                    <td className="muted">{(matchProfile ? j.matched_skills : j.top_skills ?? []).slice(0, 5).join(", ")}</td>
                  </tr>
                ))}
                {result && !result.jobs.length && <tr><td colSpan={8} className="jobs-empty">No jobs match. {idx.jobs_active ? "Loosen the filters." : "Start a scrape to fill the index."}</td></tr>}
              </tbody>
            </table>
          </div>
          {result && result.pages > 1 && (
            <div className="jobs-pager">
              <button disabled={page <= 1 || busy} onClick={() => search(page - 1)}>Previous</button>
              <span>Page {page} of {result.pages}</span>
              <button disabled={page >= result.pages || busy} onClick={() => search(page + 1)}>Next</button>
            </div>
          )}
          <p className="jobs-footnote">* No published date — this is when Agent-Hire first saw the posting.</p>
        </>
      ) : (
        <ShortlistTable api={api} leads={shortlisted} />
      )}
    </div>
  );
}

/** Pick any number of companies from the index (with their open-job counts), searchable. */
function CompanyPicker({ api, value, onChange }: { api: ApiFetch; value: string[]; onChange: (v: string[]) => void }) {
  const [all, setAll] = useState<{ slug: string; company: string; active_jobs: number }[] | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || all) return;
    void api("/api/v1/jobindex/companies", { timeoutMs: 60_000 }).then(async (r) => {
      if (!r.ok) return;
      const data = await r.json();
      setAll((data.companies ?? []).filter((c: Json) => c.active_jobs > 0)
        .sort((a: Json, b: Json) => b.active_jobs - a.active_jobs || a.company.localeCompare(b.company)));
    }).catch(() => undefined);
  }, [open, all, api]);

  useEffect(() => {
    if (!open) return;
    // Placed against the window: the filter row can scroll sideways, which would clip a dropdown inside it.
    const place = () => {
      const r = box.current?.getBoundingClientRect();
      if (r) setAnchor({ top: Math.round(r.bottom + 4), left: Math.round(Math.min(r.left, window.innerWidth - 332)) });
    };
    place();
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("resize", place);
    document.addEventListener("mousedown", onDoc);
    return () => { window.removeEventListener("resize", place); document.removeEventListener("mousedown", onDoc); };
  }, [open]);

  const names = useMemo(() => Object.fromEntries((all ?? []).map((c) => [c.slug, c.company])), [all]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = (all ?? []).filter((c) => !q || c.company.toLowerCase().includes(q) || c.slug.includes(q));
    // Picked companies stay at the top so they are easy to untick.
    return [...list.filter((c) => value.includes(c.slug)), ...list.filter((c) => !value.includes(c.slug))];
  }, [all, query, value]);
  const toggle = (slug: string) => onChange(value.includes(slug) ? value.filter((s) => s !== slug) : [...value, slug]);
  const label = !value.length ? "All companies"
    : value.length === 1 ? (names[value[0]] ?? value[0])
    : `${value.length} companies`;

  return (
    <div className="jobs-company" ref={box}>
      <button type="button" className={`jobs-company-btn ${value.length ? "on" : ""}`} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {label} ▾
      </button>
      {open && (
        <div className="jobs-company-pop" role="dialog" aria-label="Companies"
          style={anchor ? { position: "fixed", top: anchor.top, left: anchor.left } : undefined}>
          <input autoFocus value={query} onChange={(e) => setQuery(e.currentTarget.value)} placeholder="Search companies" />
          <div className="jobs-company-list">
            {!all && <div className="muted">Loading…</div>}
            {all && !shown.length && <div className="muted">No company matches “{query}”.</div>}
            {shown.map((c) => (
              <label key={c.slug} className={value.includes(c.slug) ? "on" : ""}>
                <input type="checkbox" checked={value.includes(c.slug)} onChange={() => toggle(c.slug)} />
                <span>{c.company}</span>
                <span className="muted">{c.active_jobs.toLocaleString()}</span>
              </label>
            ))}
          </div>
          <div className="jobs-company-foot">
            <span className="muted">{value.length ? `${value.length} picked` : "None picked = all companies"}</span>
            <button type="button" onClick={() => onChange([])} disabled={!value.length}>Clear</button>
          </div>
        </div>
      )}
    </div>
  );
}

type Department = { name: string; count: number; subdepartments: { code: string; name: string; count: number }[] };

/** Pick departments and/or subdepartments (the department model's 14 / 38 classes), with open-job counts. Ticking a
 * department picks all its subdepartments; the search filter takes the subdepartment codes. */
function DepartmentPicker({ api, value, onChange }: { api: ApiFetch; value: string[]; onChange: (v: string[]) => void }) {
  const [all, setAll] = useState<Department[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || all) return;
    setFailed(false);
    void api("/api/v1/jobindex/departments", { timeoutMs: 60_000 }).then(async (r) => {
      if (r.ok) setAll((await r.json()).departments ?? []);
      else setFailed(true);
    }).catch(() => setFailed(true));
  }, [open, all, api]);

  useEffect(() => {
    if (!open) return;
    // Placed against the window, like the company picker: the filter row can scroll sideways.
    const place = () => {
      const r = box.current?.getBoundingClientRect();
      if (r) setAnchor({ top: Math.round(r.bottom + 4), left: Math.round(Math.min(r.left, window.innerWidth - 332)) });
    };
    place();
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("resize", place);
    document.addEventListener("mousedown", onDoc);
    return () => { window.removeEventListener("resize", place); document.removeEventListener("mousedown", onDoc); };
  }, [open]);

  const names = useMemo(() => Object.fromEntries((all ?? []).flatMap((d) => d.subdepartments.map((s) => [s.code, s.name]))), [all]);
  const toggle = (code: string) => onChange(value.includes(code) ? value.filter((c) => c !== code) : [...value, code]);
  const toggleGroup = (d: Department) => {
    const codes = d.subdepartments.map((s) => s.code);
    const full = codes.every((c) => value.includes(c));
    onChange(full ? value.filter((c) => !codes.includes(c)) : [...new Set([...value, ...codes])]);
  };
  const fullGroups = (all ?? []).filter((d) => d.subdepartments.every((s) => value.includes(s.code)));
  const label = !value.length ? "All departments"
    : fullGroups.length === 1 && value.length === fullGroups[0].subdepartments.length ? fullGroups[0].name
    : value.length === 1 ? (names[value[0]] ?? value[0])
    : `${value.length} subdepartments`;

  return (
    <div className="jobs-company" ref={box}>
      <button type="button" className={`jobs-company-btn ${value.length ? "on" : ""}`} onClick={() => setOpen((o) => !o)} aria-expanded={open}
        title="Filter by the department model's verdict (jobs it was unsure about are left out)">
        {label} ▾
      </button>
      {open && (
        <div className="jobs-company-pop" role="dialog" aria-label="Departments"
          style={anchor ? { position: "fixed", top: anchor.top, left: anchor.left } : undefined}>
          <div className="jobs-company-list jobs-dept-list">
            {!all && !failed && <div className="muted">Loading…</div>}
            {failed && <div className="muted">The department list isn't available yet. It appears after Agent-Hire restarts with the department model.</div>}
            {(all ?? []).map((d) => {
              const codes = d.subdepartments.map((s) => s.code);
              const picked = codes.filter((c) => value.includes(c)).length;
              return (
                <div key={d.name} className="jobs-dept-group">
                  <label className={picked ? "on jobs-dept-head" : "jobs-dept-head"}>
                    <input type="checkbox" checked={picked === codes.length}
                      ref={(el) => { if (el) el.indeterminate = picked > 0 && picked < codes.length; }}
                      onChange={() => toggleGroup(d)} />
                    <span>{d.name}</span>
                    <span className="muted">{d.count.toLocaleString()}</span>
                  </label>
                  {d.subdepartments.length > 1 && d.subdepartments.map((sd) => (
                    <label key={sd.code} className={`jobs-dept-sub ${value.includes(sd.code) ? "on" : ""}`}>
                      <input type="checkbox" checked={value.includes(sd.code)} onChange={() => toggle(sd.code)} />
                      <span>{sd.name}</span>
                      <span className="muted">{sd.count.toLocaleString()}</span>
                    </label>
                  ))}
                </div>
              );
            })}
          </div>
          <div className="jobs-company-foot">
            <span className="muted">{value.length ? `${value.length} picked` : "None picked = all departments"}</span>
            <button type="button" onClick={() => onChange([])} disabled={!value.length}>Clear</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ShortlistTable({ api, leads }: { api: ApiFetch; leads: Lead[] }) {
  const [busy, setBusy] = useState<string | null>(null);
  const openResume = async (lead: Lead) => {
    const r = await api(`/api/v1/leads/${encodeURIComponent(lead.job_id)}/pdf`, { timeoutMs: 60_000 });
    if (!r.ok) return alert("No tailored résumé yet — ask the agent to tailor one.");
    const url = URL.createObjectURL(await r.blob());
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };
  const setStatus = async (lead: Lead, status: string) => {
    setBusy(lead.job_id);
    await api(`/api/v1/leads/${encodeURIComponent(lead.job_id)}/status`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
    window.dispatchEvent(new CustomEvent("leads-refresh"));
    setBusy(null);
  };
  if (!leads.length) return <div className="jobs-empty">Nothing here yet. Ask the agent to shortlist jobs, or tick jobs in “All scraped jobs”.</div>;
  return (
    <div className="jobs-table-wrap">
      <table className="jobs-table">
        <thead><tr><th>Role</th><th>Company</th><th>Stage</th><th>Résumé</th><th /></tr></thead>
        <tbody>
          {leads.map((lead) => {
            const ready = Boolean(lead.resume_asset || lead.asset);
            return (
              <tr key={lead.job_id}>
                <td>{safeUrl(lead.url) ? <a href={safeUrl(lead.url)} target="_blank" rel="noreferrer noopener">{lead.title}</a> : lead.title}</td>
                <td>{lead.company}</td>
                <td><span className={`stage stage-${lead.status}`}>{lead.status}</span></td>
                <td>{ready ? <button className="link" onClick={() => openResume(lead)}>View PDF</button> : <span className="muted">not tailored</span>}</td>
                <td className="row-actions">
                  <button disabled={busy === lead.job_id} onClick={() => setStatus(lead, "applied")}>Mark applied</button>
                  <button disabled={busy === lead.job_id} className="ghost" onClick={() => setStatus(lead, "discarded")}>Discard</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
