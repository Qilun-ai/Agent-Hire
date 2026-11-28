// Settings → Job agent: decision model endpoints (Kaggle server / API) and background scraping.
import { useEffect, useState } from "react";
import type { ApiFetch } from "../types";
import { settingsApi } from "../api/settings";
import { ApiKeyInput, useEnvironmentReport, useSecretPreviews } from "../settings/shared";

type Json = Record<string, any>;
const MASK_HINT = "saved — leave as is to keep";

const FIELDS = [
  "decision_api_url", "decision_api_key", "decision_api_model",
  "scrape_refresh_hours",
] as const;

export function AgentSettings({ api }: { api: ApiFetch }) {
  const [cfg, setCfg] = useState<Json>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<Json | null>(null);
  const [probing, setProbing] = useState(false);
  const [previews, refreshPreviews] = useSecretPreviews(api);
  const environment = useEnvironmentReport(api);
  const [status, setStatus] = useState<Json | null>(null);
  const [kaggle, setKaggle] = useState<Json | null>(null);

  useEffect(() => {
    api("/api/v1/settings").then((r) => r.json()).then((all: Json) => {
      const picked: Json = {};
      for (const key of FIELDS) picked[key] = all[key] ?? "";
      if (!picked.scrape_refresh_hours) picked.scrape_refresh_hours = "12";
      setCfg(picked);
    }).catch(() => setError("Could not load settings"));
    api("/api/v1/jobindex/status").then((r) => r.json()).then(setStatus).catch(() => {});
    api("/api/v1/jobindex/kaggle").then((r) => r.json()).then(setKaggle).catch(() => {});
  }, [api]);

  const toggleKaggle = async (enabled: boolean) => {
    const r = await api("/api/v1/jobindex/kaggle/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
    if (r.ok) setKaggle(await r.json());
    window.dispatchEvent(new CustomEvent("kaggle-changed"));
  };

  const set = (key: string, value: string) => { setCfg((c) => ({ ...c, [key]: value })); setSaved(false); };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const r = await api("/api/v1/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Save failed");
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const runProbe = async () => {
    setProbing(true);
    setProbe(null);
    await save();
    const r = await api("/api/v1/jobindex/decision/probe", { method: "POST", timeoutMs: 120_000 });
    setProbe(await r.json().catch(() => ({ error: "probe failed" })));
    setProbing(false);
  };

  const field = (key: string, label: string, placeholder: string, secret = false) => (
    <label className="ags-field">
      <span>{label}</span>
      <input
        type={secret ? "password" : "text"}
        value={cfg[key] ?? ""}
        placeholder={secret && cfg[key] ? MASK_HINT : placeholder}
        onChange={(e) => set(key, e.currentTarget.value)}
        autoComplete="off"
        spellcheck={false}
      />
    </label>
  );

  return (
    <div className="ags">
      <section>
        <h3>Decision model <span className="muted">(optional)</span></h3>
        <p className="muted">
          Classifies each scraped job: does it belong to your target role, and is it intern / junior / mid / senior / lead?
          Agent-Hire uses your Kaggle notebook first, then the decision-model API. With neither, it uses built-in rules
          (job title + required years of experience), which work fine for most searches.
        </p>
        <label className="ags-toggle">
          <input type="checkbox" checked={Boolean(kaggle?.enabled)} onChange={(e) => void toggleKaggle(e.currentTarget.checked)} />
          <span className="ags-switch" aria-hidden />
          <span>
            <b>Use a Kaggle notebook</b>
            <span className="muted"> — a “Kaggle” box appears at the top right. Paste the notebook link there each session
            (Kaggle links expire); Agent-Hire loads the decision model on the notebook's GPUs automatically.</span>
          </span>
        </label>
        {kaggle?.enabled && (
          <p className="muted ags-kaggle-state">
            Kaggle: {kaggle.ready ? "model ready" : kaggle.has_link ? (kaggle.phase ? String(kaggle.phase).replace(/_/g, " ") : "connected") : "no link yet — use the box at the top right"}
          </p>
        )}
        {environment?.decision_api.source === "environment" && (
          <p className="muted">
            Using the decision model from <code>{environment.decision_api.variable}</code> in your install folder's
            .env file. Values entered here take precedence.
          </p>
        )}
        <div className="ags-grid">
          {field("decision_api_url", "Decision model API URL (optional)", "https://… (System One /v1/systemone compatible)")}
          <label className="ags-field">
            <span>Decision model API key</span>
            <ApiKeyInput
              provider="decision"
              value={cfg.decision_api_key ?? ""}
              onChange={(v) => set("decision_api_key", v)}
              onDelete={() => { settingsApi.deleteKey(api, "decision_api_key").then(refreshPreviews).catch(() => {}); }}
              preview={previews.decision_api_key}
              placeholder={previews.decision_api_key || cfg.decision_api_key ? "Paste a new key to replace the saved one" : "Paste the API key (if the endpoint needs one)"}
            />
          </label>
          {field("decision_api_model", "Decision model name", "optional")}
        </div>
        <div className="ags-actions">
          <button onClick={runProbe} disabled={probing}>{probing ? "Testing…" : "Save & test decision model"}</button>
          {probe && (
            probe.configured === false
              ? <span className="muted">No endpoint configured — using built-in rules.</span>
              : (probe.results ?? []).map((r: Json) => (
                  <span key={r.endpoint} className={r.ok ? "ok" : "bad"}>
                    {r.endpoint}: {r.ok ? `reachable (control job → software ${Math.round((r.role_p ?? 0) * 100)}%, ${r.level ?? "?"})` : `failed (${r.error ?? "no answer"})`}
                  </span>
                ))
          )}
        </div>
      </section>

      <section>
        <h3>Background scraping</h3>
        <p className="muted">
          Agent-Hire scrapes {status?.companies_available ?? "the verified"} company career sites in the background. A full pass takes hours;
          the agent searches whatever has been collected so far. After the first scrape you start (from the chat or the Jobs tab),
          it repeats on this schedule. Set 0 to turn the automatic refresh off.
        </p>
        <div className="ags-grid">
          <label className="ags-field">
            <span>Refresh every (hours)</span>
            <input type="number" min={0} max={168} value={cfg.scrape_refresh_hours ?? "12"} onChange={(e) => set("scrape_refresh_hours", e.currentTarget.value)} />
          </label>
        </div>
        {status && <p className="muted">Index: {(status.index?.jobs_active ?? 0).toLocaleString()} open jobs from {status.index?.companies_indexed ?? 0} companies · stored in {status.jobs_dir}</p>}
      </section>

      <div className="gh-settings-save">
        {error && <span className="gh-settings-error">{error}</span>}
        <button className="btn btn-accent" onClick={save} disabled={saving}>{saved ? "Saved" : saving ? "Saving..." : "Save settings"}</button>
      </div>
    </div>
  );
}
