// Leads (kept jobs) endpoints of the local API.
import type { ApiFetch, Lead } from "../types";
import { getJson } from "./client";

export const LEADS_PAGE_SIZE = 100;

export interface LeadsSummary {
  total: number;
  counts: Record<string, number>;
  [key: string]: unknown;
}

export interface LeadsPage {
  items: Lead[];
  /** True when the page came back full, so another page may follow. */
  has_more: boolean;
}

export interface LeadsQuery {
  view?: string;
  sort?: string;
  page?: number;
  limit?: number;
  q?: string;
}

function toQuery(query: LeadsQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== "") params.set(key, String(value));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export const leadsApi = {
  async page(api: ApiFetch, query: LeadsQuery = {}): Promise<LeadsPage> {
    // With `page` the API answers {items, page, pages}; without it, a plain array of every match.
    const data = await getJson<Lead[] | { items?: Lead[]; page?: number; pages?: number }>(api, `/api/v1/leads${toQuery(query)}`);
    if (Array.isArray(data)) return { items: data, has_more: false };
    return { items: data.items ?? [], has_more: (data.page ?? 1) < (data.pages ?? 1) };
  },
  summary: (api: ApiFetch) => getJson<LeadsSummary>(api, "/api/v1/leads/summary"),
};
