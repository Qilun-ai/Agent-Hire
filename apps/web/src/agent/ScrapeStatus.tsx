// Always-visible scraper indicator in the top bar. Lives in the app shell, so switching tabs never
// resets it; polls faster while a scrape runs and slows down when idle or the page is hidden.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiFetch } from "../types";

type Json = Record<string, any>;

function ago(iso?: string | null) {
  if (!iso) return "";
  const minutes = (Date.now() - new Date(iso).getTime()) / 60_000;
  if (Number.isNaN(minutes)) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)}h ago`;
  return `${Math.round(minutes / 1440)}d ago`;
}

export function ScrapeStatus({ api, onOpen }: { api: ApiFetch; onOpen: () => void }) {
  const [status, setStatus] = useState<Json | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api("/api/v1/jobindex/scrape", { timeoutMs: 15_000 });
      if (!r.ok) throw new Error(String(r.status));
      setStatus(await r.json());
      setUnreachable(false);
    } catch {
      setUnreachable(true);
    }
  }, [api]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      await load();
      if (stopped) return;
      const running = Boolean(statusRef.current?.active);
      const delay = document.hidden ? 60_000 : running ? 5_000 : 30_000;
      timer.current = window.setTimeout(tick, delay);
    };
    void tick();
    const onVisible = () => { if (!document.hidden) { if (timer.current) window.clearTimeout(timer.current); void tick(); } };
    const onChange = () => void load(); // the Jobs tab fires this after start/stop
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("scrape-changed", onChange);
    return () => {
      stopped = true;
      if (timer.current) window.clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("scrape-changed", onChange);
    };
  }, [load]);

  const statusRef = useRef<Json | null>(null);
  statusRef.current = status;

  const active = status?.active;
  const last = (status?.recent ?? []).find((run: Json) => run.status !== "running");
  let label: string;
  let state: "live" | "idle" | "off";
  if (unreachable) {
    label = "Scraper status unavailable";
    state = "off";
  } else if (active) {
    label = `Scraping ${active.companies_done}/${active.companies_planned} · ${active.new_jobs} new`;
    state = "live";
  } else if (last) {
    label = `Last scrape ${ago(last.finished_at || last.started_at)}`;
    state = "idle";
  } else {
    label = "Not scraped yet";
    state = "idle";
  }
  const pct = active ? Math.round((active.companies_done / Math.max(1, active.companies_planned)) * 100) : 0;

  return (
    <button className={`gh-scrape gh-scrape-${state}`} onClick={onOpen} title={active ? `Started ${ago(active.started_at)} — click to open Jobs` : "Open Jobs to start or schedule a scrape"}>
      <span className="gh-scrape-dot" />
      <span className="gh-scrape-label">{label}</span>
      {active && <span className="gh-scrape-bar"><span style={{ width: `${pct}%` }} /></span>}
    </button>
  );
}
