// The saved jobs (leads) and their summary counts, refreshed when anything changes them.
import { useCallback, useEffect, useRef, useState } from "react";
import { leadsApi, type LeadsSummary } from "../api/leads";
import type { ApiFetch, Lead } from "../types";

export function useLeads(api: ApiFetch | null): { leads: Lead[]; summary: LeadsSummary | null; refresh: () => void } {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [summary, setSummary] = useState<LeadsSummary | null>(null);
  const pending = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const load = useCallback(() => {
    if (!api) return;
    leadsApi.page(api, { view: "all", sort: "newest" }).then((page) => setLeads(page.items)).catch(() => {});
    leadsApi.summary(api).then(setSummary).catch(() => {});
  }, [api]);

  // Bursts of events (a scrape, a batch shortlist) collapse into one reload.
  const refresh = useCallback(() => {
    clearTimeout(pending.current);
    pending.current = setTimeout(load, 400);
  }, [load]);

  useEffect(() => {
    load();
    const every = setInterval(load, 60_000);
    window.addEventListener("leads-refresh", refresh);
    window.addEventListener("api-event", refresh);
    return () => {
      clearInterval(every);
      clearTimeout(pending.current);
      window.removeEventListener("leads-refresh", refresh);
      window.removeEventListener("api-event", refresh);
    };
  }, [load, refresh]);

  return { leads, summary, refresh };
}
