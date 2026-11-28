// Agent-Hire shell: Agent chat · Jobs · Applications & chart · Settings, with the Companies and
// Profile panels on the right.
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import type { SettingsTab } from "./types";
import { useConnection } from "./hooks/useConnection";
import { useLeads } from "./hooks/useLeads";
import ErrorBoundary from "./ui/ErrorBoundary";
import { AgentApi } from "./agent/agentApi";
import { ChatView } from "./agent/ChatView";
import { JobsView } from "./agent/JobsView";
import { ApplicationsView } from "./applications/ApplicationsView";
import { ScrapeStatus } from "./agent/ScrapeStatus";
import { CompaniesPanel } from "./agent/CompaniesPanel";
import { KaggleBox } from "./agent/KaggleBox";
import { ClassifyStatus } from "./agent/ClassifyStatus";
import { ProfilePanel } from "./agent/ProfilePanel";

const SettingsView = lazy(() => import("./settings/SettingsView"));

type Tab = "chat" | "jobs" | "applications" | "settings";
const TABS: { id: Tab; label: string }[] = [
  { id: "chat", label: "Agent chat" },
  { id: "jobs", label: "Jobs" },
  { id: "applications", label: "Applications & chart" },
  { id: "settings", label: "Settings" },
];
const SETTINGS_TABS: SettingsTab[] = ["ai", "email", "agent", "general"];

/** The view comes from the URL hash (#jobs, #settings/email), so reload and back/forward keep it. */
function readLocation(): { tab: Tab; settings: SettingsTab } {
  const [tab, sub] = window.location.hash.replace(/^#/, "").split("/");
  const settings = SETTINGS_TABS.includes(sub as SettingsTab) ? (sub as SettingsTab) : "ai";
  if (TABS.some((item) => item.id === tab)) return { tab: tab as Tab, settings };
  try {
    const stored = localStorage.getItem("agent-hire-tab") as Tab | null;
    return { tab: stored && TABS.some((item) => item.id === stored) ? stored : "chat", settings };
  } catch {
    return { tab: "chat", settings };
  }
}

export default function App() {
  const { state, token, api, error } = useConnection();
  const agent = useMemo(() => (token === null ? null : new AgentApi(token)), [token]);
  const { leads, summary } = useLeads(api);
  const [{ tab, settings }, setLocation] = useState(readLocation);
  const [cart, setCart] = useState<string[]>([]);
  const [companiesOpen, setCompaniesOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [composeHint, setComposeHint] = useState(0);

  const go = useCallback((next: Tab, sub?: SettingsTab) => {
    const settingsTab = sub ?? "ai";
    setLocation({ tab: next, settings: settingsTab });
    const hash = next === "settings" && settingsTab !== "ai" ? `#settings/${settingsTab}` : `#${next}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    try {
      localStorage.setItem("agent-hire-tab", next);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    const onPop = () => setLocation(readLocation());
    window.addEventListener("popstate", onPop);
    window.addEventListener("hashchange", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("hashchange", onPop);
    };
  }, []);

  useEffect(() => {
    if (!api) return;
    const load = () =>
      api("/api/v1/cart")
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => data && setCart(data.job_ids ?? data.cart ?? []))
        .catch(() => {});
    void load();
    window.addEventListener("leads-refresh", load);
    return () => window.removeEventListener("leads-refresh", load);
  }, [api]);

  useEffect(() => {
    document.title = `${TABS.find((item) => item.id === tab)?.label ?? "Agent chat"} · Agent-Hire`;
  }, [tab]);

  if (!api || !agent) {
    return (
      <div className="gh-boot">
        <div className="gh-boot-card">
          <div className="gh-brand"><img className="gh-brand-logo" src="/agent-hire-logo-transparent.png" alt="" aria-hidden="true" />Agent-Hire</div>
          <p>{state === "offline" && error ? "Waiting for the Agent-Hire service to start…" : "Connecting to the local service…"}</p>
          <span className="spinner" />
        </div>
      </div>
    );
  }

  const counts = summary?.counts;
  const label = TABS.find((item) => item.id === tab)?.label ?? "View";
  return (
    <div className="gh-shell">
      <header className="gh-tabs">
        <div className="gh-brand"><img className="gh-brand-logo" src="/agent-hire-logo-transparent.png" alt="" aria-hidden="true" />Agent-Hire</div>
        <nav className="gh-tablist" role="tablist" aria-label="Sections">
          {TABS.map((item) => (
            <button key={item.id} role="tab" aria-selected={tab === item.id} className={`gh-tab ${tab === item.id ? "active" : ""}`} onClick={() => go(item.id)}>
              {item.label}
              {item.id === "jobs" && cart.length > 0 && <span className="gh-badge">{cart.length}</span>}
              {item.id === "applications" && counts && (counts.applied ?? 0) > 0 && <span className="gh-badge">{counts.applied}</span>}
            </button>
          ))}
        </nav>
        <div className="gh-tabs-spacer" />
        {state === "offline" && <span className="gh-offline" role="status">Reconnecting…</span>}
        <KaggleBox api={api} />
        <ClassifyStatus api={api} />
        <ScrapeStatus api={api} onOpen={() => setCompaniesOpen(true)} />
        <button className="gh-companies-btn" onClick={() => { setProfileOpen(false); setCompaniesOpen((open) => !open); }} aria-expanded={companiesOpen}>
          Companies
        </button>
        <button className="gh-companies-btn gh-profile-btn" onClick={() => { setCompaniesOpen(false); setProfileOpen((open) => !open); }} aria-expanded={profileOpen}>
          Profile
        </button>
      </header>
      <main className="gh-body">
        <ErrorBoundary label={label} api={api}>
          <Suspense fallback={<div className="gh-view-loading"><span className="spinner" /> Opening…</div>}>
            <div style={{ display: tab === "chat" ? "contents" : "none" }}>
              <ChatView agent={agent} onOpenSettings={() => go("settings", "ai")} focusSignal={composeHint} />
            </div>
            {tab === "jobs" && <JobsView api={api} leads={leads} />}
            {tab === "applications" && <ApplicationsView api={api} leads={leads} summary={summary} cartCount={cart.length} />}
            {tab === "settings" && <SettingsView api={api} tab={settings} onTab={(next) => go("settings", next)} />}
          </Suspense>
        </ErrorBoundary>
      </main>
      <CompaniesPanel api={api} open={companiesOpen} onClose={() => setCompaniesOpen(false)} />
      <ProfilePanel api={api} open={profileOpen} onClose={() => setProfileOpen(false)} onEditWithAgent={() => { setProfileOpen(false); go("chat"); setComposeHint((n) => n + 1); }} />
    </div>
  );
}
