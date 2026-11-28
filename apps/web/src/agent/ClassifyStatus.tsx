// Top-bar indicator: how many open jobs both job models (department + experience level) have classified.
// A job counts once both models have a verdict for it; each job goes through each model once.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiFetch } from "../types";

type Progress = {
  active: number; classified: number; remaining: number; department_done: number; level_done: number;
  models: { department: boolean; level: boolean }; device: string;
};

const WHERE: Record<string, string> = { kaggle: "on the Kaggle GPU", cuda: "on this computer's GPU", cpu: "on this computer's CPU" };

export function ClassifyStatus({ api }: { api: ApiFetch }) {
  const [p, setP] = useState<Progress | null>(null);
  const timer = useRef<number | null>(null);
  const ref = useRef<Progress | null>(null);
  ref.current = p;

  const load = useCallback(async () => {
    try {
      const r = await api("/api/v1/jobindex/encoders/progress", { timeoutMs: 20_000 });
      if (r.ok) setP(await r.json());
    } catch { /* keep the last value */ }
  }, [api]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      await load();
      if (stopped) return;
      const working = (ref.current?.remaining ?? 0) > 0;
      timer.current = window.setTimeout(tick, document.hidden ? 120_000 : working ? 10_000 : 60_000);
    };
    void tick();
    const onVisible = () => { if (!document.hidden) { if (timer.current) window.clearTimeout(timer.current); void tick(); } };
    document.addEventListener("visibilitychange", onVisible);
    return () => { stopped = true; if (timer.current) window.clearTimeout(timer.current); document.removeEventListener("visibilitychange", onVisible); };
  }, [load]);

  if (!p || !p.active || (!p.models.department && !p.models.level)) return null;
  const done = p.remaining === 0;
  const pct = Math.floor((p.classified / Math.max(1, p.active)) * 100);
  const where = WHERE[p.device] ?? "";
  const title = `${p.classified.toLocaleString()} of ${p.active.toLocaleString()} open jobs classified by both models`
    + `\nDepartment model: ${p.department_done.toLocaleString()} · Experience-level model: ${p.level_done.toLocaleString()}`
    + (done ? "\nNew jobs are classified as they arrive" : `\n${p.remaining.toLocaleString()} waiting${where ? `, running ${where}` : ""}`);

  return (
    <div className={`gh-scrape gh-scrape-${done ? "idle" : "live"}`} title={title} role="status" style={{ cursor: "default" }}>
      <span className="gh-scrape-dot" />
      {done ? `Classified ${p.classified.toLocaleString()}` : `Classifying ${p.classified.toLocaleString()}/${p.active.toLocaleString()}`}
      <span className="gh-scrape-bar" aria-label={`${pct}% classified`}><span style={{ width: `${done ? 100 : pct}%` }} /></span>
    </div>
  );
}
