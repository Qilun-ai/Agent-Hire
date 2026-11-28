// Settings endpoints of the local API.
import type { ApiFetch } from "../types";
import { getJson, postJson } from "./client";

export type SettingsMap = Record<string, unknown>;

/** Where each AI key and the decision model come from (GET /api/v1/settings/environment). */
export interface EnvironmentReport {
  /** Providers whose key is set in the environment, and the variable it came from. */
  providers: { id: string; variable: string; key_setting: string }[];
  decision_api: { source: "settings" | "environment" | "none" | string; variable?: string };
}

export const settingsApi = {
  get: (api: ApiFetch) => getJson<SettingsMap>(api, "/api/v1/settings"),
  save: (api: ApiFetch, values: SettingsMap) => postJson<SettingsMap>(api, "/api/v1/settings", values),
  previews: (api: ApiFetch) => getJson<Record<string, string>>(api, "/api/v1/settings/secret-previews"),
  environment: (api: ApiFetch) => getJson<EnvironmentReport>(api, "/api/v1/settings/environment"),
  validate: (api: ApiFetch, values: SettingsMap) => postJson<Record<string, unknown>>(api, "/api/v1/settings/validate", values, 60_000),
  models: (api: ApiFetch, provider: string) => getJson<{ models?: string[] } & Record<string, unknown>>(api, `/api/v1/settings/models/${encodeURIComponent(provider)}`),
  async deleteKey(api: ApiFetch, key: string): Promise<void> {
    const response = await api(`/api/v1/settings/key/${encodeURIComponent(key)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`Could not remove the key (${response.status})`);
  },
};
