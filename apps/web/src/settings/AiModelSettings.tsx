// Settings → AI model: the provider that writes résumés, fills forms and reads unclear emails.
import { useEffect, useMemo, useState } from "react";
import { settingsApi, type EnvironmentReport, type SettingsMap } from "../api/settings";
import type { ApiFetch } from "../types";
import { ApiKeyInput, SECRET_SET, SectionLabel, useSecretPreviews } from "./shared";

const NAMES: Record<string, string> = {
  custom: "OpenAI-compatible", anthropic: "Anthropic", opencode: "OpenCode", openai: "OpenAI", gemini: "Gemini",
  groq: "Groq", nvidia: "NVIDIA", deepseek: "DeepSeek", xai: "xAI", kimi: "Kimi", moonshot: "Moonshot", mistral: "Mistral",
  openrouter: "OpenRouter", together: "Together", fireworks: "Fireworks", cerebras: "Cerebras", perplexity: "Perplexity",
  huggingface: "Hugging Face", cohere: "Cohere", sambanova: "SambaNova", qwen: "Qwen", dashscope: "DashScope",
};
const HINTS: Record<string, string> = { custom: "Any compatible /v1 endpoint", anthropic: "Claude", opencode: "Zen / Go API" };
const SUGGESTED: Record<string, string[]> = {
  anthropic: ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
  opencode: ["muse-spark-1.3-contributor"],
};
const KEY_PLACEHOLDER: Record<string, string> = {
  anthropic: "sk-ant-…", gemini: "AIza…", nvidia: "nvapi-…", openrouter: "sk-or-…", opencode: "OpenCode API key",
};
const STATUS: Record<string, [string, string]> = {
  ok: ["ok", "OK"], invalid_key: ["bad", "Invalid key"], unreachable: ["warn", "Unreachable"], not_set: ["", "Not set"],
  not_configured: ["", "Not set"], missing_key: ["", "Not set"], unchecked: ["", "Unchecked"], model_not_found: ["bad", "Model not found"],
  bad_endpoint: ["bad", "Bad endpoint"], format_error: ["bad", "Format error"], missing_model: ["bad", "Missing model"],
  rate_limited: ["warn", "Rate limited"], server_error: ["bad", "Server error"],
};

const name = (id: string) => NAMES[id] ?? id;
const text = (value: unknown) => (typeof value === "string" ? value : value == null ? "" : String(value));

interface Report { status?: string; model?: string; latency_ms?: number; reply?: string; detail?: string }

function keySetting(id: string, environment: EnvironmentReport | null): string {
  if (id === "anthropic") return "anthropic_key";
  return environment?.providers.find((found) => found.id === id)?.key_setting ?? `${id}_api_key`;
}

export function AiModelSettings({ api }: { api: ApiFetch }) {
  const [values, setValues] = useState<SettingsMap | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [environment, setEnvironment] = useState<EnvironmentReport | null>(null);
  const [previews, refreshPreviews] = useSecretPreviews(api);
  const [busy, setBusy] = useState<"" | "saving" | "checking">("");
  const [message, setMessage] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [reports, setReports] = useState<Record<string, Report> | null>(null);

  useEffect(() => {
    settingsApi.get(api).then(setValues).catch(() => setMessage({ tone: "bad", text: "Could not load settings." }));
    settingsApi.environment(api).then(setEnvironment).catch(() => {});
  }, [api]);

  const value = (key: string) => (key in edits ? edits[key] : text(values?.[key]));
  const set = (key: string, next: string) => {
    setEdits((current) => ({ ...current, [key]: next }));
    setMessage(null);
  };

  const providers = useMemo(() => {
    const list = ["custom", "anthropic", "opencode"];
    for (const found of environment?.providers ?? []) if (!list.includes(found.id)) list.push(found.id);
    const current = text(values?.llm_provider);
    if (current && current !== "ollama" && !list.includes(current)) list.push(current);
    return list;
  }, [environment, values]);

  const provider = value("llm_provider");
  const selected = providers.includes(provider) ? provider : "";
  const variable = (id: string) => environment?.providers.find((found) => found.id === id)?.variable ?? "";

  /** Everything edited, minus empty key fields (an empty key field means "keep the stored key"). */
  const pending = (): SettingsMap => {
    const out: SettingsMap = {};
    for (const [key, next] of Object.entries(edits)) {
      if (/(_api_key|_key)$/.test(key) && !next.trim()) continue;
      out[key] = next.trim();
    }
    return out;
  };

  const save = async () => {
    setBusy("saving");
    setMessage(null);
    try {
      const next = await settingsApi.save(api, pending());
      setValues((current) => ({ ...current, ...next, ...pending() }));
      setEdits({});
      refreshPreviews();
      setMessage({ tone: "ok", text: "Saved." });
    } catch (error) {
      setMessage({ tone: "bad", text: (error as Error).message });
    } finally {
      setBusy("");
    }
  };

  const check = async () => {
    setBusy("checking");
    setReports(null);
    setMessage(null);
    try {
      setReports((await settingsApi.validate(api, { llm_provider: selected, ...pending() })) as Record<string, Report>);
    } catch (error) {
      setMessage({ tone: "bad", text: (error as Error).message });
    } finally {
      setBusy("");
    }
  };

  const removeKey = async (key: string) => {
    try {
      await settingsApi.deleteKey(api, key);
      setValues((current) => ({ ...current, [key]: "" }));
      set(key, "");
      refreshPreviews();
    } catch (error) {
      setMessage({ tone: "bad", text: (error as Error).message });
    }
  };

  const fields = (id: string) => {
    const endpoint = id === "custom" || id === "opencode";
    const key = keySetting(id, environment);
    const fromEnvironment = variable(id);
    const saved = text(values?.[key]) === SECRET_SET || Boolean(previews[key]);
    const model = `${id}_model`;
    const base = `${id}_base_url`;
    return (
      <>
        {endpoint && (
          <label className="field">
            <span className="field-label">{name(id)} endpoint URL</span>
            <input value={value(base)} onInput={(event) => set(base, (event.target as HTMLInputElement).value)}
              placeholder={id === "custom" ? "https://api.example.com/v1" : "https://opencode.ai/zen/go/v1"} spellcheck={false} />
          </label>
        )}
        <label className="field">
          <span className="field-label">API key</span>
          <ApiKeyInput
            provider={id}
            value={edits[key] ?? ""}
            onChange={(next) => set(key, next)}
            onDelete={saved ? () => void removeKey(key) : undefined}
            preview={saved ? previews[key] ?? "" : undefined}
            placeholder={fromEnvironment ? `Using ${fromEnvironment} from your environment · paste a key to override` : KEY_PLACEHOLDER[id] ?? "API key"}
          />
        </label>
        {id === "anthropic" && (
          <label className="field">
            <span className="field-label">Base URL</span>
            <input value={value(base)} onInput={(event) => set(base, (event.target as HTMLInputElement).value)}
              placeholder="https://api.anthropic.com · leave empty for official Anthropic" spellcheck={false} />
          </label>
        )}
        <label className="field">
          <span className="field-label">Model name</span>
          <input value={value(model)} onInput={(event) => set(model, (event.target as HTMLInputElement).value)} placeholder="Model ID" spellcheck={false} />
          {SUGGESTED[id] && (
            <span className="field-hint">
              common:{" "}
              {SUGGESTED[id].map((suggestion, index) => (
                <span key={suggestion}>
                  {index > 0 && ", "}
                  <button type="button" className="link-btn" onClick={() => set(model, suggestion)}>{suggestion}</button>
                </span>
              ))}
            </span>
          )}
        </label>
        {endpoint && <Advanced id={id} value={value} set={set} />}
      </>
    );
  };

  if (!values) return <div className="settings-loading">{message?.text ?? "Loading…"}</div>;
  return (
    <div className="ai-settings">
      <SectionLabel label="AI model" sub="writes résumés, fills forms and reads unclear emails" />
      <div className="settings-box">
        <div className="provider-grid" role="radiogroup" aria-label="Provider">
          {providers.map((id) => (
            <button key={id} type="button" role="radio" aria-checked={selected === id} className={`provider-card ${selected === id ? "active" : ""}`}
              onClick={() => set("llm_provider", id)}>
              <strong>{name(id)}</strong>
              <small>{variable(id) ? `key from ${variable(id)}` : HINTS[id] ?? "API key"}</small>
            </button>
          ))}
        </div>
        {selected ? fields(selected) : <p className="muted">Pick the provider you have a key for. Keys exported in your shell (for example GEMINI_API) are found automatically.</p>}
        <div className="row">
          <button type="button" className="btn" onClick={() => void check()} disabled={Boolean(busy)}>{busy === "checking" ? "Checking…" : "Check keys"}</button>
        </div>
        {reports && <Reports reports={reports} selected={selected} />}
      </div>
      <div className="settings-save">
        {message && <span className={`save-note ${message.tone}`}>{message.text}</span>}
        <button type="button" className="btn btn-accent" onClick={() => void save()} disabled={Boolean(busy)}>{busy === "saving" ? "Saving…" : "Save settings"}</button>
      </div>
    </div>
  );
}

function Advanced({ id, value, set }: { id: string; value: (key: string) => string; set: (key: string, next: string) => void }) {
  const protocolKey = `${id}_protocol`;
  const formatKey = `${id}_endpoint_type`;
  const effortKey = `${id}_reasoning_effort`;
  const protocol = value(protocolKey) || "openai";
  const format = protocol === "anthropic" ? "messages" : value(formatKey) || (id === "custom" ? "chat/completions" : "responses");
  const select = (key: string, label: string, current: string, options: [string, string][], disabled = false) => (
    <label className="field">
      <span className="field-label">{label}</span>
      <select value={current} disabled={disabled} onChange={(event) => set(key, (event.target as HTMLSelectElement).value)}>
        {options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}
      </select>
    </label>
  );
  return (
    <details className="advanced">
      <summary>Advanced: protocol, request format, thinking</summary>
      <div className="field-grid three">
        {select(protocolKey, "Protocol", protocol, [["openai", "OpenAI"], ["anthropic", "Anthropic"]])}
        {select(formatKey, "Request format", format, [["chat/completions", "Chat"], ["responses", "Responses"], ["messages", "Messages"]], protocol === "anthropic")}
        {select(effortKey, "Reasoning / thinking", value(effortKey) || "none", [["none", "None (standard)"], ["low", "Low"], ["medium", "Medium"], ["high", "High"]])}
      </div>
      <p className="field-hint">Maps to the Anthropic thinking budget, Responses reasoning effort, or Chat reasoning_effort.</p>
    </details>
  );
}

function Reports({ reports, selected }: { reports: Record<string, Report>; selected: string }) {
  const rows = Object.entries(reports).sort(([a], [b]) => Number(a !== selected) - Number(b !== selected) || name(a).localeCompare(name(b)));
  if (!rows.length) return <p className="muted">No providers to check yet.</p>;
  return (
    <div className="key-reports">
      {rows.map(([id, report]) => {
        const [tone, label] = STATUS[report.status ?? ""] ?? ["bad", report.status ?? "Unknown"];
        return (
          <div key={id} className="key-report">
            <div><strong>{name(id)}</strong> <span className={`pill ${tone}`}>{label}</span></div>
            {(report.model || report.latency_ms) && (
              <small className="muted">{[report.model, report.latency_ms ? `${report.latency_ms} ms` : ""].filter(Boolean).join(" · ")}</small>
            )}
            {report.reply && <q>{report.reply}</q>}
            {tone !== "ok" && report.detail && <small className="bad-text">{report.detail}</small>}
          </div>
        );
      })}
    </div>
  );
}
