// Profile → Application answers: what forms ask that a résumé doesn't say, grouped and editable.
import { useEffect, useMemo, useState } from "react";
import type { ApiFetch } from "../types";

interface Field { key: string; label: string; group?: string; options?: string[]; default?: string }
interface Facts {
  schema: { facts: Field[]; defaults: Field[] };
  facts: Record<string, string>;
  defaults: Record<string, string>;
}
type Namespace = "facts" | "defaults";

const LONG = new Set(["summary", "why_interested"]);

export function AnswersCard({ api }: { api: ApiFetch }) {
  const [data, setData] = useState<Facts | null>(null);
  const [draft, setDraft] = useState<{ facts: Record<string, string>; defaults: Record<string, string> }>({ facts: {}, defaults: {} });
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  useEffect(() => {
    api("/api/v1/fill/facts")
      .then((response) => response.json())
      .then((body: Facts) => {
        setData(body);
        setDraft({ facts: { ...(body.facts ?? {}) }, defaults: { ...(body.defaults ?? {}) } });
      })
      .catch(() => setNote({ tone: "bad", text: "Could not load your application answers." }));
  }, [api]);

  const groups = useMemo(() => {
    const out: { name: string; namespace: Namespace; fields: Field[] }[] = [];
    for (const field of data?.schema?.facts ?? []) {
      const name = field.group || "Other";
      const group = out.find((item) => item.name === name);
      if (group) group.fields.push(field);
      else out.push({ name, namespace: "facts", fields: [field] });
    }
    if (data?.schema?.defaults?.length) out.push({ name: "Default answers", namespace: "defaults", fields: data.schema.defaults });
    return out;
  }, [data]);

  const set = (namespace: Namespace, key: string, value: string) => {
    setDraft((current) => ({ ...current, [namespace]: { ...current[namespace], [key]: value } }));
    setNote(null);
  };

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      const response = await api("/api/v1/fill/facts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft) });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Could not save your answers.");
      setNote({ tone: "ok", text: "Saved. The extension uses these on the next form." });
    } catch (error) {
      setNote({ tone: "bad", text: (error as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="answers">
      <h3>Application answers</h3>
      <p className="answers-intro">
        What forms ask that your résumé doesn't say. The extension copies these exactly: the decision model only picks which
        answer goes in which box, and anything it can't match is written by your AI model or left for you. Empty means the form
        is left blank there.
      </p>
      {!data && note && <p className="bad-text">{note.text}</p>}
      {groups.map((group) => {
        const values = draft[group.namespace];
        const filled = group.fields.filter((field) => (values[field.key] ?? "").trim()).length;
        const id = `${group.namespace}:${group.name}`;
        const expanded = open === id;
        return (
          <div key={id} className={`answer-group ${expanded ? "open" : ""}`}>
            <button type="button" className="answer-head" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : id)}>
              <span>{group.name}</span>
              <span className="answer-count">{filled}/{group.fields.length} {expanded ? "▲" : "▼"}</span>
            </button>
            {expanded && (
              <div className="answer-fields">
                {group.fields.map((field) => {
                  const listId = field.options?.length ? `answer-${group.namespace}-${field.key}` : undefined;
                  const placeholder = field.default ? `default: ${field.default}` : undefined;
                  const value = values[field.key] ?? "";
                  return (
                    <label key={field.key} className="answer-field">
                      <span>{field.label}</span>
                      {LONG.has(field.key) ? (
                        <textarea rows={3} value={value} placeholder={placeholder} onInput={(event) => set(group.namespace, field.key, (event.target as HTMLTextAreaElement).value)} />
                      ) : (
                        <input value={value} placeholder={placeholder} list={listId} onInput={(event) => set(group.namespace, field.key, (event.target as HTMLInputElement).value)} />
                      )}
                      {listId && <datalist id={listId}>{field.options!.map((option) => <option key={option} value={option} />)}</datalist>}
                    </label>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
      {data && (
        <div className="answers-save">
          <button type="button" className="btn btn-accent" onClick={() => void save()} disabled={busy}>{busy ? "Saving…" : "Save answers"}</button>
          {note && <span className={note.tone === "ok" ? "ok-text" : "bad-text"}>{note.text}</span>}
        </div>
      )}
    </section>
  );
}
