// Top-bar Kaggle box: paste the notebook link each session (they expire), and Agent-Hire loads its models there
// automatically: the department and experience-level models (they classify new jobs on the notebook GPU) and the
// decision model (form filling). Shown only when Settings → Job agent → "Use a Kaggle notebook" is on.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiFetch } from "../types";

type Json = Record<string, any>;

const PHASES: Record<string, string> = {
  starting: "Starting…",
  installing: "Downloading the model",
  starting_models: "Starting the model on the GPUs",
  warming_models: "Warming up",
  ready: "Ready",
  failed: "Deployment failed",
  stopped: "Stopped",
  not_deployed: "Connected — model not loaded",
  unreachable: "Link expired or notebook stopped",
};

export function KaggleBox({ api }: { api: ApiFetch }) {
  const [status, setStatus] = useState<Json | null>(null);
  const [open, setOpen] = useState(false);
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  // The top bar scrolls sideways (overflow-x: auto), which also clips anything hanging below it, so the
  // popup is placed relative to the window, under the pill, instead of inside the bar.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const place = useCallback(() => {
    const r = box.current?.getBoundingClientRect();
    if (r) setAnchor({ top: Math.round(r.bottom + 2), right: Math.max(12, Math.round(window.innerWidth - r.right)) });
  }, []);
  useEffect(() => {
    if (!open) return;
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, place]);

  const load = useCallback(async (refresh = false) => {
    try {
      const r = await api(`/api/v1/jobindex/kaggle${refresh ? "?refresh=true" : ""}`, { timeoutMs: 60_000 });
      if (r.ok) setStatus(await r.json());
    } catch { /* keep the last status */ }
  }, [api]);

  useEffect(() => {
    void load();
    const onChange = () => void load(true);
    window.addEventListener("kaggle-changed", onChange);
    return () => window.removeEventListener("kaggle-changed", onChange);
  }, [load]);

  // Poll quickly while the model is being deployed, slowly once it is ready or idle.
  const phase = status?.phase as string | undefined;
  const deploying = Boolean(phase && ["starting", "installing", "starting_models", "warming_models"].includes(phase));
  useEffect(() => {
    if (!status?.enabled) return;
    const t = window.setInterval(() => void load(true), deploying ? 5_000 : 60_000);
    return () => window.clearInterval(t);
  }, [status?.enabled, deploying, load]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  if (!status?.enabled) return null;

  const connect = async (reuse = false) => {
    if (!reuse && !link.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api("/api/v1/jobindex/kaggle/connect", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link: reuse ? "" : link.trim() }), timeoutMs: 240_000,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not connect to Kaggle");
      setStatus(data);
      setLink("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const setDevice = async (device: string) => {
    const r = await api("/api/v1/jobindex/kaggle/encoder-device", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device }),
    }).catch(() => null);
    if (r?.ok) setStatus(await r.json());
  };

  const stop = async () => {
    setBusy(true);
    await api("/api/v1/jobindex/kaggle/stop", { method: "POST", timeoutMs: 90_000 }).catch(() => null);
    setBusy(false);
    void load(true);
  };

  const forget = async () => {
    setBusy(true);
    await api("/api/v1/jobindex/kaggle/forget", { method: "POST" }).catch(() => null);
    setBusy(false);
    window.dispatchEvent(new CustomEvent("kaggle-changed"));
    void load(true);
  };

  const ready = phase === "ready";
  const bad = phase === "failed" || phase === "unreachable" || !status.has_link;
  const pct = Math.round((status.progress ?? 0) * 100);
  const label = !status.has_link ? "Paste Kaggle link"
    : ready ? `Kaggle ready${status.replicas ? ` · ${status.replicas}×GPU` : ""}`
    : phase === "installing" ? `Kaggle: downloading ${pct}%`
    : deploying ? `Kaggle: ${PHASES[phase!] ?? phase}`
    : phase === "unreachable" ? "Kaggle link expired"
    : `Kaggle: ${PHASES[phase ?? ""] ?? "connecting"}`;

  return (
    <div className="kg" ref={box}>
      <button className={`kg-pill ${ready ? "ok" : bad ? "bad" : deploying ? "live" : ""}`} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="kg-dot" />
        {label}
      </button>
      {open && (
        <div className="kg-pop" role="dialog" aria-label="Kaggle notebook"
          style={anchor ? { position: "fixed", top: anchor.top, right: anchor.right } : undefined}>
          <div className="kg-title">Kaggle notebook</div>
          <p className="muted">
            Paste your notebook's connection link (Kaggle links expire, so paste a fresh one each session). Agent-Hire loads its
            models on the notebook's GPUs automatically: the department and experience-level models classify new jobs there, and
            the decision model fills application forms. Enable GPU (T4 ×2) and Internet in the notebook first.
          </p>
          <div className="kg-row">
            <input
              type="password"
              autoComplete="off"
              spellcheck={false}
              placeholder="https://…jupyter-proxy.kaggle.net?token=…"
              value={link}
              onChange={(e) => setLink(e.currentTarget.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void connect(); }}
            />
            <button onClick={() => void connect()} disabled={busy || !link.trim()}>{busy ? "Connecting…" : "Connect"}</button>
          </div>
          {error && <div className="kg-error">{error}</div>}
          {status.has_link && (
            <div className="kg-status">
              <div><b>{PHASES[phase ?? ""] ?? phase ?? "Unknown"}</b>{status.gpus?.length ? <span className="muted"> · {status.gpus.length} GPU</span> : null}</div>
              {(phase === "installing" || phase === "starting") && (
                <>
                  <div className="tc-progress"><div style={{ width: `${pct}%` }} /></div>
                  <div className="muted">{pct}% of 9.5 GB · first load takes ~10 minutes, later loads in the same session are quick</div>
                </>
              )}
              {deploying && phase !== "installing" && phase !== "starting" && <div className="muted">Almost there…</div>}
              {status.classifiers?.phase && (
                <div className="muted">
                  Job models: <b>{status.classifiers.phase === "ready" ? "ready" : status.classifiers.phase === "failed" ? "failed" : "loading…"}</b>
                  {status.classifiers.phase === "ready" && status.classifiers.jobs_per_s ? ` · ~${Math.round(status.classifiers.jobs_per_s)} jobs/s` : ""}
                  {status.classifiers.phase === "ready" ? " · new jobs are classified on the notebook GPU" : ""}
                </div>
              )}
              <label className="kg-device muted" title="Auto: the notebook only takes a big backlog (1,000+ jobs); new jobs from scrapes run on this computer">
                Job models run on{" "}
                <select value={status.encoder_device ?? "auto"} disabled={busy} onChange={(e) => void setDevice(e.currentTarget.value)}>
                  <option value="auto">Auto (Kaggle for big backlogs)</option>
                  <option value="kaggle">Kaggle notebook</option>
                  <option value="local">This computer</option>
                </select>
              </label>
              {status.classifiers?.phase === "failed" && <div className="kg-error">{status.classifiers.error || "The job models failed to load; jobs are classified on this computer instead."}</div>}
              {ready && <div className="muted">Decision model ready for form filling.</div>}
              {phase === "failed" && <div className="kg-error">{status.error || "The deployment failed."}{status.log_tail ? <pre>{status.log_tail}</pre> : null}</div>}
              {phase === "unreachable" && <div className="kg-error">{status.error}</div>}
              {(phase === "not_deployed" || phase === "stopped" || phase === "failed") && (
                <button className="kg-stop" onClick={() => void connect(true)} disabled={busy}>Load model with the saved link</button>
              )}
              {(ready || deploying) && <button className="kg-stop" onClick={stop} disabled={busy}>Stop model</button>}
              <button className="kg-stop" onClick={() => void forget()} disabled={busy}
                title="Remove the saved notebook link (Kaggle links expire with the session)">Forget link</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
