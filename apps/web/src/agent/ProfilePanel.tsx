// Right-side panel with everything in the user's profile. Read-only: the agent edits it on request.
import { useCallback, useEffect, useState } from "react";
import type { ApiFetch } from "../types";
import { AnswersCard } from "../profile/AnswersCard";
import { safeUrl } from "../lib/url";

type Json = Record<string, any>;

function bullets(text: string): string[] {
  return String(text || "")
    .split(/\n+/)
    .map((line) => line.replace(/^\s*[-•*]\s*/, "").trim())
    .filter(Boolean);
}

function Links({ identity }: { identity: Json }) {
  const items: [string, string | undefined][] = [
    ["Email", identity.email ? safeUrl(`mailto:${identity.email}`) : undefined],
    ["LinkedIn", safeUrl(identity.linkedin_url)],
    ["GitHub", safeUrl(identity.github_url)],
    ["Website", safeUrl(identity.website_url)],
  ];
  const shown = items.filter(([, href]) => href);
  return (
    <div className="pp-links">
      {identity.city && <span className="muted">📍 {identity.city}</span>}
      {identity.phone && <span className="muted">☎ {identity.phone}</span>}
      {shown.map(([label, href]) => (
        <a key={label} href={href} target="_blank" rel="noreferrer noopener">{label}</a>
      ))}
    </div>
  );
}

export function ProfilePanel({ api, open, onClose, onEditWithAgent }: { api: ApiFetch; open: boolean; onClose: () => void; onEditWithAgent: () => void }) {
  const [profile, setProfile] = useState<Json | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api("/api/v1/profile", { timeoutMs: 20_000 });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setProfile(await r.json());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [api]);

  useEffect(() => {
    if (!open) return;
    void load();
    const onChange = () => void load();
    window.addEventListener("profile-changed", onChange);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("profile-changed", onChange); window.removeEventListener("keydown", onKey); };
  }, [open, load, onClose]);

  const p = profile ?? {};
  const skills: Json[] = p.skills ?? [];
  const groups = skills.reduce<Record<string, string[]>>((acc, s) => {
    const cat = String(s.cat ?? s.category ?? "general") || "general";
    (acc[cat] ||= []).push(String(s.n ?? s.name ?? ""));
    return acc;
  }, {});
  const exp: Json[] = p.exp ?? [];
  const projects: Json[] = p.projects ?? [];
  const list = (key: string): string[] => (p[key] ?? []).map((x: Json | string) => (typeof x === "string" ? x : x.title ?? x.name ?? "")).filter(Boolean);
  const empty = profile && !p.n && !skills.length && !exp.length && !projects.length;

  return (
    <>
      <div className={`cp-backdrop ${open ? "open" : ""}`} onClick={onClose} />
      <aside className={`cp pp ${open ? "open" : ""}`} aria-hidden={!open} aria-label="Profile">
        <header className="cp-head">
          <div>
            <h2>{p.n || "Your profile"}</h2>
            {p.identity && <Links identity={p.identity} />}
          </div>
          <button className="cp-close" onClick={onClose} aria-label="Close">×</button>
        </header>
        <div className="pp-body">
          {error && <div className="kg-error">Could not load the profile: {error}</div>}
          {!profile && !error && <div className="muted">Loading…</div>}
          {empty && (
            <div className="pp-empty">
              <p>Nothing here yet. Attach your résumé in Agent chat and the agent fills this in.</p>
            </div>
          )}
          {p.s && <section><h3>Summary</h3><p>{p.s}</p></section>}
          {open && <AnswersCard api={api} />}
          {skills.length > 0 && (
            <section>
              <h3>Skills <span className="muted">{skills.length}</span></h3>
              {Object.entries(groups).map(([cat, names]) => (
                <div key={cat} className="pp-skillgroup">
                  <div className="pp-cat">{cat}</div>
                  <div className="tc-chips">{names.map((n) => <span key={n} className="tc-chip">{n}</span>)}</div>
                </div>
              ))}
            </section>
          )}
          {exp.length > 0 && (
            <section>
              <h3>Experience <span className="muted">{exp.length}</span></h3>
              {exp.map((e) => (
                <div key={e.id ?? `${e.role}-${e.co}`} className="pp-item">
                  <div className="pp-item-head"><b>{e.role}</b>{e.co ? ` · ${e.co}` : ""}<span className="muted">{e.period}</span></div>
                  {bullets(e.d ?? e.description).length > 0 && <ul>{bullets(e.d ?? e.description).map((b, i) => <li key={i}>{b}</li>)}</ul>}
                </div>
              ))}
            </section>
          )}
          {projects.length > 0 && (
            <section>
              <h3>Projects <span className="muted">{projects.length}</span></h3>
              {projects.map((x) => (
                <div key={x.id ?? x.title} className="pp-item">
                  <div className="pp-item-head">
                    <b>{x.title}</b>
                    {safeUrl(x.repo) && <a href={safeUrl(x.repo)} target="_blank" rel="noreferrer noopener">repo</a>}
                  </div>
                  {x.stack && <div className="muted pp-stack">{Array.isArray(x.stack) ? x.stack.join(", ") : x.stack}</div>}
                  {x.impact && <p>{x.impact}</p>}
                </div>
              ))}
            </section>
          )}
          {[["education", "Education"], ["certifications", "Certifications"], ["achievements", "Achievements"]].map(([key, label]) =>
            list(key).length ? (
              <section key={key}>
                <h3>{label}</h3>
                <ul>{list(key).map((t, i) => <li key={i}>{t}</li>)}</ul>
              </section>
            ) : null,
          )}
        </div>
        <footer className="cp-foot">
          <span className="muted">Ask the agent to add, change or delete anything here.</span>
          <button onClick={onEditWithAgent}>Edit with the agent</button>
        </footer>
      </aside>
    </>
  );
}
