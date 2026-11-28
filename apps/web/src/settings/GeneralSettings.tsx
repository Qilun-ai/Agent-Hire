// Settings → General: appearance, activity log, about, and deleting local data.
import { useState } from "react";
import { getJson, postJson } from "../api/client";
import type { ApiFetch } from "../types";
import Icon from "../ui/Icon";
import { readTheme, setTheme, type ThemeChoice } from "../lib/theme";
import { SectionLabel } from "./shared";

interface ActivityRow { job_id?: string; action?: string; ts?: string }

const THEMES: { id: ThemeChoice; label: string; icon: string }[] = [
  { id: "light", label: "Light", icon: "sun" },
  { id: "dark", label: "Dark", icon: "moon" },
  { id: "system", label: "System", icon: "globe" },
];

export function GeneralSettings({ api }: { api: ApiFetch }) {
  const [theme, setThemeState] = useState<ThemeChoice>(readTheme);
  const [activity, setActivity] = useState<ActivityRow[] | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [clearSettings, setClearSettings] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetResult, setResetResult] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const choose = (choice: ThemeChoice) => {
    setTheme(choice);
    setThemeState(choice);
  };

  const openActivity = () => {
    if (activity) return setActivity(null);
    getJson<ActivityRow[]>(api, "/api/v1/events?limit=100")
      .then((rows) => { setActivity(Array.isArray(rows) ? rows : []); setActivityError(null); })
      .catch((error: Error) => setActivityError(error.message));
  };

  const reset = async () => {
    setResetting(true);
    setResetResult(null);
    try {
      await postJson(api, "/api/v1/data/reset", { confirm: "DELETE", clear_settings: clearSettings }, 120_000);
      setResetResult({ tone: "ok", text: "Your local data was deleted. Reload the app to start fresh." });
      setConfirm("");
    } catch (error) {
      setResetResult({ tone: "bad", text: (error as Error).message });
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="general-settings">
      <SectionLabel label="Appearance" sub="theme used across the app — System follows your OS" />
      <div className="theme-grid" role="radiogroup" aria-label="Theme">
        {THEMES.map((item) => (
          <button key={item.id} type="button" role="radio" aria-checked={theme === item.id} className={`theme-btn ${theme === item.id ? "active" : ""}`} onClick={() => choose(item.id)}>
            <Icon name={item.icon} size={15} />
            {item.label}
          </button>
        ))}
      </div>

      <SectionLabel label="Activity log" sub="a technical record of recent background work" />
      <div>
        <button type="button" className="btn" onClick={openActivity} aria-expanded={Boolean(activity)}>
          <Icon name="activity" size={14} />
          {activity ? "Hide activity log" : "Open activity log"}
        </button>
        {activityError && <p className="bad-text">Could not load the activity log: {activityError}</p>}
        {activity && (
          <div className="activity-log">
            {activity.length === 0 ? <p className="muted">Background activity will appear here.</p> : (
              <table>
                <thead><tr><th>Time</th><th>What happened</th></tr></thead>
                <tbody>
                  {activity.map((row, index) => (
                    <tr key={index}>
                      <td className="mono nowrap">{(row.ts ?? "").replace("T", " ").slice(0, 19)}</td>
                      <td>{(row.action ?? "").replace(/^status_changed=/, "Status changed to ")}{row.job_id && row.job_id !== "__system__" ? <span className="muted"> · {row.job_id}</span> : null}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      <SectionLabel label="About & license" sub="free software: you can use, change and share it" />
      <p className="about-text">
        Agent-Hire, copyright (C) 2026 noobed-max and Agent-Hire contributors. Licensed under the{" "}
        <a href="https://www.gnu.org/licenses/agpl-3.0.en.html" target="_blank" rel="noreferrer noopener">GNU AGPL v3.0</a> only, with no warranty.
        <br />
        Source code:{" "}
        <a href="https://github.com/Qilun-ai/Agent-Hire" target="_blank" rel="noreferrer noopener">github.com/Qilun-ai/Agent-Hire</a>.
      </p>

      <SectionLabel label="Danger zone" sub="wipe local data to start fresh — this cannot be undone" />
      <div className="danger-box">
        <div className="danger-row">
          <p>
            Delete your saved jobs, profile and generated documents on this device. The scraped job index, your settings,
            provider keys and job sources are kept unless you also choose a full reset below.
          </p>
          {!resetOpen && (
            <button type="button" className="btn danger-outline" onClick={() => setResetOpen(true)}>
              <Icon name="trash" size={14} />
              Delete all data
            </button>
          )}
        </div>
        {resetOpen && (
          <div className="danger-confirm">
            <label className="check">
              <input type="checkbox" checked={clearSettings} onChange={(event) => setClearSettings((event.target as HTMLInputElement).checked)} />
              <span>Also reset settings, provider keys, preferences and job source choices (no backup is kept of these)</span>
            </label>
            <p className="muted">A backup of the local database is written first.</p>
            <label className="field">
              <span className="field-label">Type DELETE to confirm</span>
              <input value={confirm} onInput={(event) => setConfirm((event.target as HTMLInputElement).value)} autoComplete="off" />
            </label>
            <div className="row">
              <button type="button" className="btn danger-solid" disabled={confirm !== "DELETE" || resetting} onClick={() => void reset()}>
                {resetting ? "Deleting…" : "Delete all data"}
              </button>
              <button type="button" className="btn" onClick={() => { setResetOpen(false); setConfirm(""); setResetResult(null); }}>Cancel</button>
              {resetResult?.tone === "ok" && <button type="button" className="btn" onClick={() => location.reload()}>Reload app</button>}
            </div>
            {resetResult && <p className={resetResult.tone === "ok" ? "ok-text" : "bad-text"}>{resetResult.text}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
