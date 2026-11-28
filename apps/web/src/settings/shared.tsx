// Building blocks shared by the settings panels.
import { useCallback, useEffect, useState } from "react";
import { settingsApi, type EnvironmentReport } from "../api/settings";
import type { ApiFetch } from "../types";

/** The value the API returns in place of a stored secret. */
export const SECRET_SET = "__AGENT_HIRE_SECRET_SET__";

export function SectionLabel({ label, sub }: { label: string; sub?: string }) {
  return (
    <div className="section-label">
      <strong>{label}</strong>
      {sub && <span>{sub}</span>}
    </div>
  );
}

/** The first characters of each stored key, for "saved" hints that never reveal the key. */
export function useSecretPreviews(api: ApiFetch): [Record<string, string>, () => void] {
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const refresh = useCallback(() => {
    settingsApi.previews(api).then(setPreviews).catch(() => {});
  }, [api]);
  useEffect(refresh, [refresh]);
  return [previews, refresh];
}

export function useEnvironmentReport(api: ApiFetch): EnvironmentReport | null {
  const [report, setReport] = useState<EnvironmentReport | null>(null);
  useEffect(() => {
    settingsApi.environment(api).then(setReport).catch(() => {});
  }, [api]);
  return report;
}

/** A password field for an API key, with a delete button once a key is stored. */
export function ApiKeyInput({ provider, value, onChange, onDelete, preview, placeholder }: {
  provider: string;
  value: string;
  onChange: (value: string) => void;
  onDelete?: () => void;
  preview?: string;
  placeholder?: string;
}) {
  const saved = Boolean(preview) || value === SECRET_SET;
  return (
    <div className="key-input">
      <input
        type="password"
        name={`${provider}-key`}
        value={value === SECRET_SET ? "" : value}
        placeholder={saved && preview ? `${preview}•••••••• saved — paste a new key to replace it` : placeholder}
        onInput={(event) => onChange((event.target as HTMLInputElement).value)}
        autoComplete="new-password"
        spellcheck={false}
      />
      {saved && onDelete && (
        <button type="button" className="btn btn-sm danger-soft" onClick={onDelete}>Delete key</button>
      )}
    </div>
  );
}
