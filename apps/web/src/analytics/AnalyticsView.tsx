import { useEffect, useMemo, useState } from "react";
import { SankeyChart } from "./SankeyChart";
import {
  applicationCounts,
  applicationCountsFromSummary,
  buildFlowDataFromCounts,
  buildFlowStatsFromCounts,
  type FlowStat,
} from "./analyticsData";
import { leadsApi, LEADS_PAGE_SIZE, type LeadsSummary } from "../api/leads";
import type { ApiFetch, Lead } from "../types";

const LEGEND: Array<{ label: string; color: string }> = [
  { label: "Awaiting reply", color: "var(--yellow)" },
  { label: "Interviewing", color: "var(--teal)" },
  { label: "Accepted", color: "var(--ok)" },
  { label: "Rejected", color: "var(--bad)" },
];

/**
 * Every saved application, fetched on demand. The shared `leads` array is only
 * the first bounded page, so a funnel built from it would silently drop every
 * application beyond the window (S8A-C6). Each request stays bounded
 * (`view=applied` server-side, page-sized); the walk stops when the server
 * says there is no next page — complete by construction, never a guess.
 */
export async function fetchAllApplications(api: ApiFetch): Promise<{ items: Lead[]; complete: boolean }> {
  const all: Lead[] = [];
  let page = 1;
  // Guard against a pathological `has_more` that never clears — well past any
  // realistic application count. Hitting it while more pages exist is an
  // explicit incomplete verdict, never silently "all applications" (S8A-R3-C3).
  const MAX_APPLICATION_PAGES = 50;
  for (;;) {
    const data = await leadsApi.page(api, { view: "applied", sort: "newest", page, limit: LEADS_PAGE_SIZE });
    all.push(...data.items);
    if (!data.has_more) return { items: all, complete: true };
    if (page >= MAX_APPLICATION_PAGES) return { items: all, complete: false };
    page += 1;
  }
}

/** Which scope the rendered funnel actually covers. */
type FunnelScope = "summary" | "all-applications" | "capped-applications" | "loaded-window" | "loading";

/** Saved → cart → applied: the jobs that have not been applied to yet, next to the funnel. */
export function savedStats(summary: LeadsSummary | null | undefined, cartCount: number): FlowStat[] {
  if (!summary) return cartCount ? [{ label: "In Apply Cart", value: String(cartCount), sub: "set aside to apply", tone: "info" }] : [];
  return [
    { label: "Jobs Saved", value: String(summary.total), sub: "found and kept", tone: "neutral" },
    { label: "In Apply Cart", value: String(cartCount), sub: "set aside to apply", tone: "info" },
    { label: "Hidden", value: String(summary.counts.discarded ?? 0), sub: "not interested", tone: "warn" },
  ];
}

export function AnalyticsView({ api, leads, summary, cartCount = 0 }: {
  api: ApiFetch | null;
  /** Jobs in the apply cart (saved, not applied yet). */
  cartCount?: number;
  /** The shared bounded page — last-resort fallback only, labelled as such. */
  leads: Lead[];
  /** SQL-aggregated counts for the whole saved set (App's /leads/summary). */
  summary?: LeadsSummary | null;
}) {
  const [fetchedApps, setFetchedApps] = useState<Lead[] | null>(null);
  const [appsComplete, setAppsComplete] = useState(false);
  const [appsFailed, setAppsFailed] = useState(false);

  // On-demand applications read — only when the summary endpoint is absent
  // (older server). Each page is bounded; the walker's own verdict decides
  // whether the aggregate covered the whole saved set or stopped at the guard.
  useEffect(() => {
    if (!api || summary) return;
    let alive = true;
    fetchAllApplications(api)
      .then(({ items, complete }) => {
        if (!alive) return;
        setFetchedApps(items);
        setAppsComplete(complete);
      })
      .catch(() => {
        if (alive) setAppsFailed(true);
      });
    const onRefresh = () => {
      setAppsFailed(false);
      fetchAllApplications(api)
        .then(({ items, complete }) => {
          if (!alive) return;
          setFetchedApps(items);
          setAppsComplete(complete);
        })
        .catch(() => { if (alive) setAppsFailed(true); });
    };
    window.addEventListener("leads-refresh", onRefresh);
    return () => {
      alive = false;
      window.removeEventListener("leads-refresh", onRefresh);
    };
  }, [api, summary]);

  const scope: FunnelScope = summary
    ? "summary"
    : fetchedApps !== null
      ? appsComplete ? "all-applications" : "capped-applications"
      : appsFailed || !api
        ? "loaded-window"
        : "loading";

  const counts = useMemo(() => {
    if (summary) return applicationCountsFromSummary(summary);
    if (fetchedApps !== null) return applicationCounts(fetchedApps);
    if (scope === "loaded-window") return applicationCounts(leads);
    return null;
  }, [summary, fetchedApps, scope, leads]);

  const stats = useMemo(() => (counts ? buildFlowStatsFromCounts(counts) : []), [counts]);
  const saved = useMemo(() => savedStats(summary, cartCount), [summary, cartCount]);
  const savedTiles = saved.map(stat => (
    <div key={stat.label} className={`card an-stat tone-${stat.tone}`}>
      <span>{stat.label}</span>
      <strong>{stat.value}</strong>
      <em>{stat.sub}</em>
    </div>
  ));
  const flow = useMemo(
    () => (counts ? buildFlowDataFromCounts(counts) : { nodes: [], links: [] }),
    [counts],
  );

  if (scope === "loading") {
    return (
      <div className="gh-page scroll analytics-page">
        <div className="an-grid">
          <section className="card an-hero">
            <span className="eyebrow">Pipeline health</span>
            <h2>Counting your applications…</h2>
            <p>Reading every saved application, not just the first page.</p>
          </section>
        </div>
      </div>
    );
  }

  if (!counts || counts.total === 0) {
    return (
      <div className="gh-page scroll analytics-page">
        <div className="an-grid">
          <section className="card an-hero">
            <span className="eyebrow">Pipeline health</span>
            <h2>No applications yet</h2>
            <p>
              These charts track every job you actually apply to. Move a job into
              <strong> Applications</strong> from the pipeline and its journey — replies,
              interviews, offers — shows up here with real numbers, never placeholders.
            </p>
          </section>
          {savedTiles}
        </div>
      </div>
    );
  }

  return (
    <div className="gh-page scroll analytics-page">
      <div className="an-grid">
        <section className="card an-hero">
          <span className="eyebrow">Pipeline health</span>
          <h2>Where your applications stand</h2>
          <p>
            Every application traced from send-off to final decision — replies, interview
            rounds, offers, and the stages that leak the most candidates. Hover any stage
            or flow ribbon for exact numbers.
          </p>
        </section>

        {savedTiles}
        {stats.map(stat => (
          <div key={stat.label} className={`card an-stat tone-${stat.tone}`}>
            <span>{stat.label}</span>
            <strong>{stat.value}</strong>
            <em>{stat.sub}</em>
          </div>
        ))}
      </div>

      <section className="card an-chart-card">
        <div className="an-chart-head">
          <div>
            <span className="eyebrow">Funnel</span>
            <h3>Application flow</h3>
          </div>
          <div className="an-legend">
            {LEGEND.map(item => (
              <span key={item.label}>
                <i style={{ background: item.color }} />
                {item.label}
              </span>
            ))}
          </div>
        </div>

        <SankeyChart nodes={flow.nodes} links={flow.links} />

        <p className="an-footnote">
          {scope === "loaded-window" ? (
            <>
              Partial view — only the {counts.total} application{counts.total === 1 ? "" : "s"} in the
              currently loaded page are counted; the full saved set may be larger.
            </>
          ) : scope === "capped-applications" ? (
            <>
              Partial view — counted the first {counts.total} application{counts.total === 1 ? "" : "s"} before the
              page guard stopped the walk; the saved set is larger.
            </>
          ) : (
            <>
              Applications flow left to right: first whether they got a reply, then how each
              interview ended. Ribbons are colored by their stage and width is proportional to
              the number of applications; stage totals sit beside each node. Data
              covers every saved application, not just the loaded page.
            </>
          )}
          {counts.accepted > counts.acceptedAfterInterview && (
            <>
              {" "}
              {counts.accepted - counts.acceptedAfterInterview} accepted application
              {counts.accepted - counts.acceptedAfterInterview === 1 ? " has" : "s have"} no interview on
              record and {counts.accepted - counts.acceptedAfterInterview === 1 ? "is" : "are"} shown under
              Interviewing, since an offer implies one.
            </>
          )}
        </p>
      </section>
    </div>
  );
}
