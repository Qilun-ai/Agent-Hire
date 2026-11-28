// Agent Chat: history sidebar + streaming conversation with the Agent-Hire agent.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AgentApi, ChatEvent, DisplayItem, Schedule, SessionSummary } from "./agentApi";
import { Markdown } from "./Markdown";
import { ToolCard } from "./ToolCard";

interface Props {
  agent: AgentApi;
  onOpenSettings: () => void;
  /** Changes when another view asks for the composer ("Edit with the agent"). */
  focusSignal?: number;
}

const SUGGESTIONS = [
  "Here's my résumé — I'm a fresher looking for software engineering roles.",
  "Find software engineer jobs posted in the last 24 hours that match my skills.",
  "Scrape new jobs and check back every 3 hours with anything that fits me.",
  "How are my applications doing?",
];

function applyEvent(items: DisplayItem[], event: ChatEvent): DisplayItem[] {
  const next = items.slice();
  const lastPending = () => {
    for (let i = next.length - 1; i >= 0; i--) if (next[i].kind === "assistant" && next[i].pending) return i;
    return -1;
  };
  switch (event.type) {
    case "user":
      next.push({ kind: "user", text: String(event.text ?? ""), source: event.source as DisplayItem["source"] });
      return next;
    case "assistant_start":
      next.push({ kind: "assistant", text: "", pending: true });
      return next;
    case "text_delta": {
      let i = lastPending();
      if (i < 0) { next.push({ kind: "assistant", text: "", pending: true }); i = next.length - 1; }
      next[i] = { ...next[i], text: (next[i].text ?? "") + String(event.delta ?? "") };
      return next;
    }
    case "assistant_end": {
      const i = lastPending();
      const finalText = String(event.text ?? "");
      const error = event.error ? String(event.error) : undefined;
      if (i >= 0) {
        if (!finalText.trim() && !error) next.splice(i, 1);
        else next[i] = { kind: "assistant", text: finalText, error };
      } else if (finalText.trim() || error) next.push({ kind: "assistant", text: finalText, error });
      return next;
    }
    case "tool_start":
      next.push({ kind: "tool", id: String(event.id), name: String(event.name), args: event.args });
      return next;
    case "tool_end": {
      const i = next.findIndex((item) => item.kind === "tool" && item.id === event.id);
      const patch = { result: String(event.result ?? ""), details: event.details, isError: Boolean(event.isError) };
      if (i >= 0) next[i] = { ...next[i], ...patch };
      else next.push({ kind: "tool", id: String(event.id), name: String(event.name), ...patch });
      return next;
    }
    case "error":
      next.push({ kind: "assistant", text: "", error: String(event.text ?? "Something went wrong") });
      return next;
    case "notice":
      next.push({ kind: "assistant", text: `_${String(event.text ?? "")}_` });
      return next;
    default:
      return items;
  }
}

function groupByDay(sessions: SessionSummary[]) {
  const groups: { label: string; items: SessionSummary[] }[] = [];
  const today = new Date().toDateString();
  const yesterday = new Date(Date.now() - 86_400_000).toDateString();
  for (const s of sessions) {
    const day = new Date(s.updated_at).toDateString();
    const label = day === today ? "Today" : day === yesterday ? "Yesterday" : new Date(s.updated_at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const group = groups.find((g) => g.label === label);
    if (group) group.items.push(s);
    else groups.push({ label, items: [s] });
  }
  return groups;
}

export function ChatView({ agent, onOpenSettings, focusSignal = 0 }: Props) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [filter, setFilter] = useState("");
  const [activeId, setActiveId] = useState<string | null>(() => {
    try { return localStorage.getItem("gh-agent-active"); } catch { return null; }
  });
  const [items, setItems] = useState<DisplayItem[]>([]);
  const [running, setRunning] = useState(false);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<{ upload_id: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [llm, setLlm] = useState<{ configured: boolean; provider?: string; model?: string; reason?: string } | null>(null);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  const refreshSessions = useCallback(() => {
    agent.sessions().then((list) => { setSessions(list ?? []); }).catch((e) => setError((e as Error).message));
  }, [agent]);

  const refreshSchedules = useCallback(() => {
    if (!activeId) return setSchedules([]);
    agent.schedules(activeId).then(setSchedules).catch(() => {});
  }, [agent, activeId]);

  useEffect(() => {
    refreshSessions();
    agent.status().then((s) => setLlm(s.llm)).catch(() => {});
    return agent.streamHost((event) => {
      if (event.type === "sessions_changed" || event.type === "session_running") refreshSessions();
    });
  }, [agent, refreshSessions]);

  // Load the transcript, then follow live events from its cursor.
  useEffect(() => {
    try { if (activeId) localStorage.setItem("gh-agent-active", activeId); else localStorage.removeItem("gh-agent-active"); } catch { /* ignore */ }
    if (!activeId) { setItems([]); setRunning(false); return; }
    let stop: (() => void) | undefined;
    let cancelled = false;
    const load = () => {
      stop?.();
      agent.transcript(activeId).then((t) => {
        if (cancelled) return;
        setItems(t.items);
        setRunning(t.running);
        stickToBottom.current = true;
        stop = agent.streamSession(activeId, t.seq, (event) => {
          if (event.type === "resync") return load();
          if (event.type === "run_start") setRunning(true);
          if (event.type === "run_end") { setRunning(false); refreshSchedules(); }
          if (event.type === "tool_end" && (event.name === "start_scrape" || event.name === "scrape_status")) {
            window.dispatchEvent(new CustomEvent("scrape-changed"));
          }
          if (event.type === "tool_end" && ["save_profile", "edit_profile", "import_document"].includes(String(event.name))) {
            window.dispatchEvent(new CustomEvent("profile-changed"));
          }
          if (event.type === "tool_end" && ["add_custom_job", "delete_jobs", "update_job_status", "reset_jobs", "shortlist_jobs"].includes(String(event.name))) {
            window.dispatchEvent(new CustomEvent("leads-refresh"));
          }
          setItems((prev) => applyEvent(prev, event));
        });
      }).catch(() => {
        if (cancelled) return;
        setActiveId(null);
      });
    };
    load();
    refreshSchedules();
    return () => { cancelled = true; stop?.(); };
  }, [agent, activeId, refreshSchedules]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  useEffect(() => {
    if (!focusSignal) return;
    setDraft((current) => current || "Update my profile: ");
    setTimeout(() => textarea.current?.focus(), 50);
  }, [focusSignal]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const send = async (text?: string) => {
    const message = (text ?? draft).trim();
    if (!message && !attachments.length) return;
    setError(null);
    try {
      let id = activeId;
      if (!id) {
        id = await agent.createSession();
        setActiveId(id);
        // the effect will load the transcript; give it a beat to subscribe before sending
        await new Promise((r) => setTimeout(r, 150));
      }
      stickToBottom.current = true;
      await agent.send(id, message, attachments.map((a) => a.upload_id));
      setDraft("");
      setAttachments([]);
      refreshSessions();
      textarea.current?.focus();
    } catch (e) {
      setError((e as Error).message);
      agent.status().then((s) => setLlm(s.llm)).catch(() => {});
    }
  };

  const onFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files).slice(0, 5)) {
        const up = await agent.upload(file);
        setAttachments((prev) => [...prev, { upload_id: up.upload_id, name: up.name }]);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const newChat = () => {
    setActiveId(null);
    setItems([]);
    setDraft("");
    textarea.current?.focus();
  };

  const removeChat = async (id: string) => {
    if (!confirm("Delete this chat and its scheduled check-ins?")) return;
    await agent.deleteSession(id).catch(() => {});
    if (id === activeId) newChat();
    refreshSessions();
  };

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? sessions.filter((s) => s.title.toLowerCase().includes(q)) : sessions;
  }, [sessions, filter]);

  return (
    <div className="chat">
      <aside className="chat-side">
        <button className="chat-new" onClick={newChat}>+ New chat</button>
        <input className="chat-search" placeholder="Search chats" value={filter} onChange={(e) => setFilter(e.currentTarget.value)} />
        <div className="chat-history-label">History</div>
        <nav className="chat-history">
          {visible.length === 0 && <div className="chat-empty-history">{sessions.length ? "No matching chats" : "Your chats will appear here"}</div>}
          {groupByDay(visible).map((group) => (
            <div key={group.label}>
              <div className="chat-day">{group.label}</div>
              {group.items.map((s) => (
                <div key={s.id} className={`chat-item ${s.id === activeId ? "active" : ""}`}>
                  <button className="chat-item-btn" onClick={() => setActiveId(s.id)} title={s.title}>
                    {s.running && <span className="chat-running" aria-label="working" />}
                    <span className="chat-item-title">{s.title}</span>
                  </button>
                  <button className="chat-item-del" onClick={() => removeChat(s.id)} aria-label="Delete chat">×</button>
                </div>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <section className="chat-main">
        {llm && !llm.configured && (
          <div className="chat-banner">
            <span>{llm.reason || "Add an AI provider key to start chatting."}</span>
            <button onClick={onOpenSettings}>Open Settings</button>
          </div>
        )}
        <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
          {items.length === 0 ? (
            <div className="chat-welcome">
              <h1>What can I find for you?</h1>
              <p>Attach your résumé, tell me the role you want, and I'll find fresh jobs, tailor your résumé for each one, and load them into the extension so you only press apply.</p>
              <div className="chat-suggestions">
                {SUGGESTIONS.map((s) => <button key={s} onClick={() => (s.startsWith("Here's") ? (setDraft(s), fileInput.current?.click()) : send(s))}>{s}</button>)}
              </div>
            </div>
          ) : (
            <div className="chat-thread">
              {items.map((item, index) => {
                if (item.kind === "user") {
                  if (item.source === "schedule") {
                    return <div key={index} className="msg-schedule">⏰ Scheduled check-in — {String(item.text).replace(/^\[Scheduled check-in[^\]]*\]\s*/, "")}</div>;
                  }
                  const [body, ...files] = String(item.text).split(/\n(?=\[Attached file)/);
                  return (
                    <div key={index} className="msg msg-user">
                      <div className="msg-bubble">
                        {body.startsWith("[Attached file") ? null : body}
                        {[...(body.startsWith("[Attached file") ? [body] : []), ...files].map((f, n) => (
                          <div key={n} className="msg-file">📎 {f.replace(/^\[Attached file: /, "").replace(/ \(.*$/, "")}</div>
                        ))}
                      </div>
                    </div>
                  );
                }
                if (item.kind === "tool") return <div key={item.id ?? index} className="msg msg-tool"><ToolCard item={item} /></div>;
                return (
                  <div key={index} className="msg msg-agent">
                    {item.text ? <Markdown text={item.text} /> : item.pending ? <span className="typing"><i /><i /><i /></span> : null}
                    {item.error && <div className="msg-error">{item.error}{/key|provider|Settings/i.test(item.error) && <button onClick={onOpenSettings}>Settings</button>}</div>}
                  </div>
                );
              })}
              {running && !items.some((i) => i.pending) && <div className="msg msg-agent"><span className="typing"><i /><i /><i /></span></div>}
            </div>
          )}
        </div>

        {schedules.length > 0 && (
          <div className="chat-schedules">
            {schedules.map((s) => (
              <span key={s.id} className="chat-schedule" title={s.instruction}>
                ⏰ {s.repeatEveryMinutes ? `every ${s.repeatEveryMinutes >= 60 ? Math.round(s.repeatEveryMinutes / 60) + "h" : s.repeatEveryMinutes + "m"}` : "once"} · next {new Date(s.nextRunAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                <button onClick={() => agent.cancelSchedule(s.id).then(refreshSchedules)} aria-label="Cancel check-in">×</button>
              </span>
            ))}
          </div>
        )}

        <div className="chat-composer">
          {error && <div className="chat-error">{error}</div>}
          {attachments.length > 0 && (
            <div className="chat-attachments">
              {attachments.map((a) => (
                <span key={a.upload_id} className="chat-attachment">📎 {a.name}<button onClick={() => setAttachments((prev) => prev.filter((x) => x.upload_id !== a.upload_id))}>×</button></span>
              ))}
            </div>
          )}
          <div className="chat-input-row">
            <button className="chat-attach" onClick={() => fileInput.current?.click()} disabled={uploading} title="Attach résumé or cover letter">{uploading ? "…" : "📎"}</button>
            <input ref={fileInput} type="file" hidden multiple accept=".pdf,.docx,.doc,.txt,.md,.tex,.rtf" onChange={(e) => onFiles(e.currentTarget.files)} />
            <textarea
              ref={textarea}
              rows={1}
              placeholder="Message Agent-Hire…"
              value={draft}
              onChange={(e) => setDraft(e.currentTarget.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }}
            />
            {running ? (
              <button className="chat-send chat-stop" onClick={() => activeId && agent.abort(activeId)}>Stop</button>
            ) : (
              <button className="chat-send" onClick={() => void send()} disabled={!draft.trim() && !attachments.length}>Send</button>
            )}
          </div>
          {llm?.configured && <div className="chat-model">{llm.provider} · {llm.model}</div>}
        </div>
      </section>
    </div>
  );
}
